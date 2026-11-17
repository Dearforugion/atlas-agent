"""Живая панель ATLAS для интерактивного чата.

Слева — состояние агента: план задачи, прогресс, помощники, инструменты,
активность и ресурсы. Справа — диалог с потоковым markdown и карточками
инструментов. Снизу — собственная строка ввода с историей, автодополнением
и прокруткой диалога.

Панель включается только в настоящем терминале. Для скриптов, пайпов и
слабых терминалов остаётся классический построчный режим. Отключить панель
можно переменной окружения ATLAS_NO_PANEL=1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .knowledge import Lessons, Memory, Skills
from .profiles import PROFILES
from .scope import load_scope
from .settings import Settings
from .tools import tool_specs

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_COLOR = not os.environ.get("NO_COLOR")
_UNICODE = "utf" in (getattr(sys.stdout, "encoding", "") or "").lower()

RESET = "\033[0m"
BOLD = "1"
DIM = "2"
REVERSE = "7"

CYAN = "36"
BLUE = "34"
MAGENTA = "35"
GREEN = "32"
YELLOW = "33"
RED = "31"
WHITE = "97"

_G = (
    {
        "tl": "┌",
        "tr": "┐",
        "bl": "└",
        "br": "┘",
        "h": "─",
        "v": "│",
        "dot": "●",
        "ell": "…",
        "arrow": "▸",
        "check": "✓",
        "cross": "✗",
        "circle": "○",
        "bullet": "•",
        "mark": "▌",
        "gear": "⚙",
    }
    if _UNICODE
    else {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "h": "-",
        "v": "|",
        "dot": "*",
        "ell": "...",
        "arrow": ">",
        "check": "+",
        "cross": "x",
        "circle": "o",
        "bullet": "-",
        "mark": "|",
        "gear": "*",
    }
)

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if _UNICODE else "|/-\\"
_SPARK = "▁▂▃▄▅▆▇█" if _UNICODE else ".:-=+*#"
_BAR_FULL, _BAR_EMPTY = ("█", "░") if _UNICODE else ("#", "-")

_LOGO = (
    [
        "  ▄▀█ ▀█▀ █░░ ▄▀█ █▀",
        "  █▀█ ░█░ █▄▄ █▀█ ▄█",
    ]
    if _UNICODE
    else [
        "  A T L A S",
        "  =========",
    ]
)

COMMANDS = (
    "/help",
    "/reset",
    "/yes",
    "/agents",
    "/agent",
    "/model",
    "/memory",
    "/lessons",
    "/skills",
    "/skill",
    "/use",
    "/teach",
    "/scope",
    "/session",
    "/plan",
    "/stats",
    "/workers",
    "/worker",
    "/export",
    "/clear",
    "/exit",
)

_EXIT_RE = re.compile(r"^\s*exit code:\s*(-?\d+)", re.IGNORECASE | re.MULTILINE)
_IMPORTANT_HINTS = (
    "error",
    "exception",
    "traceback",
    "failed",
    "failure",
    "not found",
    "denied",
    "timeout",
    "cannot",
    "successfully",
    "installed",
    "готово",
    "успешно",
    "ошибка",
    "не удалось",
    "не найден",
)

_INLINE_RE = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|"
    r"\[[^\]\n]+\]\([^)\n]+\))"
)


# ---------------------------------------------------------------------------
# ANSI, ширина и мелкие помощники
# ---------------------------------------------------------------------------


def _style(text: str, code: str) -> str:
    if not _COLOR or not code:
        return text
    return f"\033[{code}m{text}{RESET}"


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


def _vlen(text: str) -> int:
    return len(_strip_ansi(text))


def _clip(text: str, width: int) -> str:
    """Обрезает строку по видимой ширине, не ломая ANSI-последовательности."""
    if width <= 0:
        return ""
    if _vlen(text) <= width:
        return text
    out: list[str] = []
    used = 0
    index = 0
    while index < len(text) and used < width:
        if text[index] == "\033":
            match = re.match(r"\033\[[0-9;]*m", text[index:])
            if match:
                out.append(match.group(0))
                index += len(match.group(0))
                continue
        out.append(text[index])
        used += 1
        index += 1
    if _COLOR and "\033[" in text:
        out.append(RESET)
    return "".join(out)


def _pad(text: str, width: int) -> str:
    text = _clip(text, width)
    return text + " " * max(0, width - _vlen(text))


def _wrap(text: str, width: int) -> list[str]:
    width = max(1, int(width))
    if not str(text or "").strip():
        return [""]
    result: list[str] = []
    for raw in str(text).splitlines():
        if not raw.strip():
            result.append("")
            continue
        result.extend(
            textwrap.wrap(
                raw,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [""]
        )
    return result or [""]


def _hard_wrap(text: str, width: int) -> list[str]:
    width = max(1, int(width))
    return [text[index : index + width] for index in range(0, len(text), width)] or [""]


def _bar(value: int, total: int, width: int, color: str = GREEN) -> str:
    width = max(1, int(width))
    if total <= 0:
        return _style(_BAR_EMPTY * width, DIM)
    filled = max(0, min(width, int(round(width * value / total))))
    return _style(_BAR_FULL * filled, color) + _style(_BAR_EMPTY * (width - filled), DIM)


def _mini_bar(value: int, total: int, width: int = 7) -> str:
    if total <= 0:
        return _style(_BAR_EMPTY * width, DIM)
    filled = max(0, min(width, int(round(width * value / total))))
    return _style(_BAR_FULL * filled, CYAN) + _style(_BAR_EMPTY * (width - filled), DIM)


def _spark(values: list[float]) -> str:
    if not values:
        return ""
    top = max(values) or 1.0
    chars = []
    for value in values:
        ratio = max(0.0, min(1.0, float(value) / top))
        chars.append(_SPARK[min(len(_SPARK) - 1, int(ratio * (len(_SPARK) - 1)))])
    return "".join(chars)


def _icon(status: str) -> tuple[str, str]:
    status = str(status or "pending").lower()
    if status == "done":
        return _G["check"], GREEN
    if status == "active":
        return _G["arrow"], YELLOW
    if status == "failed":
        return _G["cross"], RED
    return _G["circle"], DIM


def _seconds(value: float) -> str:
    value = max(0.0, float(value or 0.0))
    if value < 60:
        return f"{value:.1f}с"
    minutes = int(value // 60)
    rest = int(round(value - minutes * 60))
    if rest >= 60:
        minutes += 1
        rest = 0
    return f"{minutes}м {rest:02d}с"


def _inline_md(text: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(0)
        if token.startswith("`"):
            return _style(token[1:-1], "33")
        if token.startswith("**") or token.startswith("__"):
            return _style(token[2:-2], BOLD)
        if token.startswith("~~"):
            return _style(token[2:-2], DIM)
        if token.startswith("[") and "](" in token:
            label, _, url = token[1:-1].partition("](")
            return _style(label, "4") + " " + _style(f"({url})", DIM)
        if token.startswith("*"):
            return _style(token[1:-1], "3")
        return token

    return _INLINE_RE.sub(repl, str(text))


def _markdown_lines(text: str, width: int) -> list[str]:
    """Лёгкий построчный markdown: заголовки, списки, цитаты, код, таблицы."""
    width = max(8, int(width))
    lines: list[str] = []
    fence = False
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            if fence:
                lines.append(_style("╰" + _G["h"] * max(0, width - 1), DIM))
                fence = False
            else:
                lang = stripped[3:].strip()
                title = "код" + (f" · {lang}" if lang else "")
                fill = max(0, width - _vlen(title) - 4)
                lines.append(
                    _style("╭─ ", DIM)
                    + _style(title, DIM)
                    + _style(" " + _G["h"] * fill, DIM)
                )
                fence = True
            continue
        if fence:
            for chunk in _hard_wrap(raw, width - 2):
                lines.append(_style("│ ", DIM) + _style(chunk, CYAN))
            continue
        if not stripped:
            lines.append("")
            continue
        if re.fullmatch(r"[\s\-*_=]{3,}", stripped):
            lines.append(_style(_G["h"] * min(width, 46), DIM))
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if heading:
            for chunk in _wrap(heading.group(2), width):
                lines.append(_style(_inline_md(chunk), "1;" + CYAN))
            continue
        quote = re.match(r"^\s*>\s?(.*)$", raw)
        if quote:
            for chunk in _wrap(quote.group(1), width - 2):
                lines.append(_style("│ ", CYAN) + _inline_md(chunk))
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if bullet:
            indent = " " * min(len(bullet.group(1)), 6)
            prefix = indent + _style(_G["bullet"], CYAN) + " "
            used = len(indent) + 2
            for index, chunk in enumerate(_wrap(bullet.group(2), width - used)):
                lines.append((prefix if index == 0 else " " * used) + _inline_md(chunk))
            continue
        numbered = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", raw)
        if numbered:
            indent = " " * min(len(numbered.group(1)), 6)
            marker = numbered.group(2)
            prefix = indent + _style(marker, CYAN) + " "
            used = len(indent) + len(marker) + 1
            for index, chunk in enumerate(_wrap(numbered.group(3), width - used)):
                lines.append((prefix if index == 0 else " " * used) + _inline_md(chunk))
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            lines.append(_clip(_style("│ " + " │ ".join(cells), DIM), width))
            continue
        for chunk in _wrap(raw, width):
            lines.append(_inline_md(chunk))
    return lines


def _exit_code(output: str) -> int | None:
    match = _EXIT_RE.search(str(output or ""))
    return int(match.group(1)) if match else None


def _preview_lines(output: str, limit: int = 3) -> list[str]:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if len(lines) <= limit:
        return lines
    chosen: set[int] = {0, len(lines) - 1}
    for index, line in enumerate(lines):
        if len(chosen) >= limit:
            break
        low = line.lower()
        if any(hint in low for hint in _IMPORTANT_HINTS):
            chosen.add(index)
    for index in range(len(lines)):
        if len(chosen) >= limit:
            break
        chosen.add(index)
    return [lines[index] for index in sorted(chosen)[:limit]]


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(120, 34))
    return int(size.columns), int(size.lines)


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    kind: str
    text: str = ""
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    cache_width: int = -1
    cache: list[str] = field(default_factory=list)


@dataclass
class ToolStat:
    calls: int = 0
    ok: int = 0
    fail: int = 0
    seconds: float = 0.0


@dataclass
class Worker:
    number: int
    task: str = ""
    status: str = "running"
    started: float = field(default_factory=time.monotonic)
    seconds: float = 0.0
    tools: int = 0
    last: str = ""
    brief: str = ""
    phase: str = "запуск"


# ---------------------------------------------------------------------------
# Рендерер событий агента
# ---------------------------------------------------------------------------


class PanelStatus:
    """Совместимость с ConsoleRenderer.status для подтверждений и REPL."""

    def __init__(self, panel: "PanelSession") -> None:
        self.panel = panel

    @property
    def interactive(self) -> bool:
        return True

    def begin(self, label: str, estimate: float = 0.0) -> None:
        self.panel.phase = str(label or "")
        self.panel.working = True

    def end(self) -> None:
        return

    def write(self, text: str) -> None:
        self.panel.say(_strip_ansi(str(text)))


class PanelRenderer:
    """Превращает события Agent в состояние живой панели."""

    def __init__(self, panel: "PanelSession") -> None:
        self.panel = panel
        self.settings = panel.settings
        self.status = PanelStatus(panel)

    @property
    def interactive(self) -> bool:
        return True

    def answer_visible(self, text: str) -> bool:
        return True

    def summary(self) -> str:
        return ""

    def handle(self, kind: str, data: dict) -> None:
        panel = self.panel
        with panel.lock:
            try:
                self._handle(str(kind), dict(data or {}))
            except Exception:
                # Панель не должна ронять агента из-за ошибки отрисовки.
                pass

    # ------------------------------------------------------------------ events
    def _handle(self, kind: str, data: dict) -> None:
        panel = self.panel
        try:
            depth = int(data.get("depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        worker = data.get("worker")
        try:
            worker_no = int(worker) if worker not in (None, "") else 0
        except (TypeError, ValueError):
            worker_no = 0

        if kind == "start":
            if depth == 0:
                if panel.turn_started <= 0 or panel.turn_finished:
                    panel.begin_turn("")
                panel.working = True
                panel.turn_finished = False
                panel.step = 0
                panel.limit_reached = False
                panel.phase = "подготовка"
                panel.answer_raw = ""
                panel.answer_active = False
            else:
                self._worker_progress(worker_no, "подключился")
            return

        if kind == "step":
            if depth == 0:
                try:
                    panel.step = int(data.get("step") or 0)
                except (TypeError, ValueError):
                    panel.step = 0
                panel.working = True
                panel.phase = "думаю"
            else:
                self._worker_progress(
                    worker_no, f"думает, шаг {data.get('step') or '?'}"
                )
            return

        if kind == "token":
            if depth != 0:
                self._worker_progress(worker_no, "печатает ответ")
                return
            text = str(data.get("text") or "")
            if not text:
                return
            if not panel.answer_active:
                panel.answer_active = True
                panel.stream_started = time.monotonic()
            panel.answer_raw += text
            panel.tokens += len(text)
            elapsed = time.monotonic() - panel.stream_started
            panel.stream_rate = panel.tokens / max(0.001, elapsed)
            panel.phase = "печатаю ответ"
            return

        if kind == "llm_done":
            if depth == 0:
                seconds = float(data.get("seconds") or 0.0)
                panel.llm_seconds += max(0.0, seconds)
                if seconds > 0:
                    panel.step_durations.append(seconds)
                    if len(panel.step_durations) > 40:
                        del panel.step_durations[:20]
                panel.phase = "анализирую"
            return

        if kind == "tool_call":
            self._tool_call(data, depth, worker_no)
            return

        if kind == "tool_result":
            self._tool_result(data, depth, worker_no)
            return

        if kind == "plan":
            if depth == 0:
                self._plan(data)
            return

        if kind.startswith("subagent_"):
            self._subagent(kind, data, depth)
            return

        if kind == "notice":
            text = str(data.get("text") or "").strip()
            if text:
                if depth > 0 and worker_no:
                    text = f"помощник #{worker_no}: {text}"
                panel.add_entry("notice", text)
                panel.recent.append((datetime.now().strftime("%H:%M"), text, YELLOW))
                del panel.recent[:-8]
            return

        if kind == "limit":
            panel.limit_reached = True
            panel.add_entry("notice", f"достигнут лимит шагов: {panel.max_steps}")
            return

        if kind == "answer":
            if depth == 0:
                text = str(data.get("text") or "")
                self._finalize_answer(text)
                self._finish_turn("готово")
            return

        if kind == "end":
            if depth == 0:
                self._finalize_answer("")
                label = "лимит шагов" if panel.limit_reached else "прервано"
                self._finish_turn(label if not panel.turn_finished else "готово")
            return

    # ------------------------------------------------------------------- tools
    def _worker_progress(self, worker_no: int, phase: str) -> None:
        """Обновляет живую фазу помощника и его карточку в диалоге."""
        if not worker_no:
            return
        panel = self.panel
        worker = panel.workers.get(worker_no)
        if worker is not None:
            worker.phase = str(phase or "")
        entry = panel.worker_entries.get(worker_no)
        if entry is not None and entry.meta.get("status") in ("queued", "running"):
            entry.meta["phase"] = str(phase or "")
            entry.cache_width = -1
            entry.cache = []

    def _tool_call(self, data: dict, depth: int, worker_no: int) -> None:
        panel = self.panel
        name = str(data.get("name") or "инструмент")
        args = data.get("args") or {}
        try:
            args_text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_text = str(args)
        try:
            repeat = int(data.get("repeat") or 1)
        except (TypeError, ValueError):
            repeat = 1

        if depth > 0:
            worker = panel.workers.get(worker_no)
            if worker:
                worker.tools += 1
                worker.last = name
                self._worker_progress(worker_no, f"инструмент {name}")
            return

        self._finalize_answer("")
        entry = Entry(
            "tool",
            "",
            meta={
                "name": name,
                "args": args_text,
                "status": "running",
                "repeat": repeat,
                "seconds": 0.0,
                "preview": [],
                "total": 0,
            },
        )
        panel.add_object(entry)
        panel.current_tool = entry
        stat = panel.tool_stats.setdefault(name, ToolStat())
        stat.calls += 1
        panel.tool_calls += 1
        panel.phase = name
        panel.working = True
        panel.step_log.append(
            {"name": name, "status": "running", "seconds": 0.0}
        )
        if len(panel.step_log) > 20:
            del panel.step_log[:10]

    def _tool_result(self, data: dict, depth: int, worker_no: int) -> None:
        panel = self.panel
        name = str(data.get("name") or "инструмент")
        ok = bool(data.get("ok"))
        seconds = float(data.get("seconds") or 0.0)
        output = str(data.get("output") or "")
        code = _exit_code(output) if name in ("run_shell", "run_python") else None
        if not ok:
            label, color = "ошибка", RED
        elif code not in (None, 0):
            label, color = f"exit {code}", YELLOW
        else:
            label, color = "ok", GREEN

        if depth > 0:
            worker = panel.workers.get(worker_no)
            if worker:
                worker.last = f"{name}: {label}"
                self._worker_progress(worker_no, f"после {name}: думает")
            return

        entry = panel.current_tool
        if entry is None or entry.kind != "tool" or entry.meta.get("name") != name:
            entry = next(
                (
                    item
                    for item in reversed(panel.entries)
                    if item.kind == "tool"
                    and item.meta.get("name") == name
                    and item.meta.get("status") == "running"
                ),
                None,
            )
        preview_lines = _preview_lines(output, 3)
        total = sum(1 for line in output.splitlines() if line.strip())
        if entry is None:
            entry = Entry(
                "tool",
                "",
                meta={
                    "name": name,
                    "args": "",
                    "status": "done",
                    "repeat": 1,
                    "seconds": seconds,
                },
            )
            panel.add_object(entry)
        entry.meta.update(
            {
                "ok": ok,
                "status": "done",
                "label": label,
                "color": color,
                "seconds": seconds,
                "preview": preview_lines,
                "total": total,
                "output": output,
            }
        )
        entry.cache_width = -1
        entry.cache = []
        for item in reversed(panel.step_log):
            if item.get("name") == name and item.get("status") == "running":
                item["status"] = "ok" if ok and code in (None, 0) else "error"
                item["seconds"] = seconds
                break
        stat = panel.tool_stats.setdefault(name, ToolStat())
        if ok:
            stat.ok += 1
        else:
            stat.fail += 1
        stat.seconds += max(0.0, seconds)
        panel.tool_ok += 1 if ok else 0
        panel.tool_fail += 0 if ok else 1
        panel.tool_seconds += max(0.0, seconds)
        panel.last_tool = (name, label, seconds)
        panel.current_tool = None
        panel.phase = "думаю"
        panel.recent.append(
            (datetime.now().strftime("%H:%M"), f"{name} · {label}", color)
        )
        del panel.recent[:-8]
        panel.refresh_mutable_counts(name)
        panel.invalidate()

    # ------------------------------------------------------------------- plan
    def _plan(self, data: dict) -> None:
        panel = self.panel
        raw = data.get("tasks") or []
        if not isinstance(raw, list):
            return
        plan: list[dict] = []
        for item in raw[:30]:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                status = str(item.get("status") or "pending").lower()
                note = str(item.get("note") or "")
            else:
                text = str(item or "").strip()
                status = "pending"
                note = ""
            if not text:
                continue
            if status not in ("pending", "active", "done", "failed"):
                status = "pending"
            plan.append({"text": text[:300], "status": status, "note": note})
        previous = [str(item.get("text") or "") for item in panel.plan]
        texts = [item["text"] for item in plan]
        panel.plan = plan
        if plan and (not panel.plan_announced or texts != previous):
            panel.plan_announced = True
            panel.add_entry("plan", "", items=[dict(item) for item in plan])
            panel.recent.append(
                (datetime.now().strftime("%H:%M"), f"план: {len(plan)} шагов", CYAN)
            )
            del panel.recent[:-8]
        panel.invalidate()

    # --------------------------------------------------------------- subagents
    def _worker(self, worker_no: int) -> Worker:
        panel = self.panel
        worker = panel.workers.get(worker_no)
        if worker is None:
            worker = Worker(number=worker_no)
            panel.workers[worker_no] = worker
        return worker

    def _worker_entry(
        self,
        worker_no: int,
        task: str = "",
        brief: str = "",
        status: str = "running",
    ) -> Entry:
        """Одна живая карточка помощника: очередь -> работа -> результат."""
        panel = self.panel
        with panel.lock:
            entry = panel.worker_entries.get(worker_no)
            if entry is None:
                entry = Entry("worker", "", meta={"worker": worker_no})
                panel.worker_entries[worker_no] = entry
                panel.add_object(entry)
            panel.turn_worker_ids.add(worker_no)
            entry.meta.update(
                {
                    "worker": worker_no,
                    "task": task or entry.meta.get("task") or "",
                    "brief": brief or entry.meta.get("brief") or "",
                    "status": status,
                }
            )
            entry.cache_width = -1
            entry.cache = []
            return entry

    def _subagent(self, kind: str, data: dict, depth: int) -> None:
        panel = self.panel
        try:
            worker_no = int(data.get("worker") or 0)
        except (TypeError, ValueError):
            worker_no = 0
        if kind == "subagent_start":
            count = int(data.get("count") or 0)
            workers = int(data.get("workers") or 0)
            panel.add_entry(
                "helper",
                f"запускаю помощников: {count} задач · {workers} одновременно",
            )
            panel.phase = "помощники"
            return
        if not worker_no:
            return
        if kind == "subagent_task_queued":
            task = str(data.get("task") or "").strip()
            brief = str(data.get("brief") or "").strip()
            worker = self._worker(worker_no)
            worker.task = task or worker.task
            worker.brief = brief or worker.brief
            worker.status = "queued"
            worker.phase = "в очереди"
            entry = self._worker_entry(worker_no, worker.task, worker.brief, "queued")
            entry.meta["phase"] = "ждёт свободный слот"
            panel.recent.append(
                (datetime.now().strftime("%H:%M"), f"помощник #{worker_no} в очереди", BLUE)
            )
            del panel.recent[:-8]
            return
        if kind == "subagent_task_start":
            task = str(data.get("task") or "").strip()
            brief = str(data.get("brief") or "").strip()
            worker = self._worker(worker_no)
            worker.task = task or worker.task
            worker.brief = brief or worker.brief
            worker.status = "running"
            worker.phase = "подключился"
            worker.started = time.monotonic()
            entry = self._worker_entry(worker_no, worker.task, worker.brief, "running")
            entry.meta["phase"] = "подключился"
            if panel.working:
                panel.phase = f"помощник #{worker_no}"
            panel.recent.append(
                (datetime.now().strftime("%H:%M"), f"помощник #{worker_no} начал работу", MAGENTA)
            )
            del panel.recent[:-8]
            return
        if kind == "subagent_task_done":
            output = str(data.get("output") or "")
            worker = self._worker(worker_no)
            worker.status = "done" if data.get("ok") else "failed"
            worker.seconds = float(data.get("seconds") or 0.0)
            worker.task = worker.task or str(data.get("task") or "")
            worker.phase = "готово" if worker.status == "done" else "ошибка"
            entry = self._worker_entry(
                worker_no, worker.task, worker.brief, worker.status
            )
            entry.meta.update(
                {
                    "seconds": worker.seconds,
                    "output": output,
                    "label": worker.phase,
                }
            )
            entry.cache_width = -1
            entry.cache = []
            color = GREEN if worker.status == "done" else RED
            panel.recent.append(
                (
                    datetime.now().strftime("%H:%M"),
                    f"помощник #{worker_no} · {worker.phase} · {_seconds(worker.seconds)}",
                    color,
                )
            )
            del panel.recent[:-8]
            if panel.working:
                panel.phase = "помощники"
            return
        if kind == "subagent_end":
            ok_count = int(data.get("ok") or 0)
            failed = int(data.get("failed") or 0)
            seconds = float(data.get("seconds") or 0.0)
            panel.add_entry(
                "helper_end",
                f"помощники: {ok_count} ок · {failed} с ошибкой · {_seconds(seconds)}",
                failed=failed,
            )
            panel.phase = "думаю"
            return

    # ------------------------------------------------------------------ finish
    def _finalize_answer(self, final_text: str = "") -> None:
        panel = self.panel
        raw = panel.answer_raw
        panel.answer_raw = ""
        panel.answer_active = False
        if raw.strip():
            panel.add_entry("answer", raw)
        if final_text.strip() and final_text.strip() != raw.strip():
            panel.add_entry("answer", final_text)

    def _finish_turn(self, label: str) -> None:
        panel = self.panel
        if panel.turn_finished:
            return
        panel.turn_finished = True
        panel.working = False
        panel.sync_plan()
        elapsed = max(0.0, time.monotonic() - panel.turn_started) if panel.turn_started else 0.0
        panel.last_turn_seconds = elapsed
        done = sum(1 for item in panel.plan if item.get("status") == "done")
        gaps = [
            (index, str(item.get("text") or ""))
            for index, item in enumerate(panel.plan, 1)
            if str(item.get("status") or "pending") != "done"
        ]
        if label == "готово" and gaps:
            label = "завершено"
            names = ", ".join(
                f"{index}. {_clip(text, 60)}" for index, text in gaps[:6]
            )
            panel.add_entry(
                "notice",
                f"план выполнен на {done}/{len(panel.plan)}: шаги {names} "
                "не отмечены как done — результат не подтверждён инструментами.",
            )
        panel.phase = label
        plan_bit = f" · план {done}/{len(panel.plan)}" if panel.plan else ""
        if gaps:
            plan_bit += f" (не закрыто {len(gaps)})"
        helpers_bit = ""
        if panel.turn_worker_ids:
            workers = [
                panel.workers[wid]
                for wid in panel.turn_worker_ids
                if wid in panel.workers
            ]
            done_helpers = sum(1 for worker in workers if worker.status == "done")
            active = sum(
                1 for worker in workers if worker.status in ("queued", "running")
            )
            failed = sum(1 for worker in workers if worker.status == "failed")
            helpers_bit = f" · помощников {len(workers)} (готово {done_helpers}"
            if active:
                helpers_bit += f", в работе {active}"
            if failed:
                helpers_bit += f", ошибок {failed}"
            helpers_bit += ")"
        color = GREEN if label == "готово" else YELLOW
        panel.add_entry(
            "summary",
            f"{label} за {_seconds(elapsed)} · шагов {panel.step} · "
            f"инструментов {panel.tool_calls}{plan_bit}{helpers_bit}",
            color=color,
        )
        panel.invalidate()


# ---------------------------------------------------------------------------
# Полноэкранная панель
# ---------------------------------------------------------------------------


class PanelSession:
    """Полноэкранный чат ATLAS с живой левой панелью."""

    def __init__(self, settings: Settings, auto: bool = False, force: bool = False) -> None:
        self.settings = settings
        self.auto = auto
        self.force = force
        self.lock = threading.RLock()
        self.entries: list[Entry] = []
        self.renderer = PanelRenderer(self)
        self.agent = None
        self.session_log_path: Path | None = None
        self.last_export: Path | None = None

        self.active = False
        self._out = sys.stdout
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self.cols, self.rows = _size()
        self.frame = 0

        self.session_started = time.monotonic()
        self.turn_started = 0.0
        self.last_turn_seconds = 0.0
        self.turns = 0
        self.task = ""
        self.working = False
        self.turn_finished = True
        self.phase = "готов"
        self.step = 0
        self.max_steps = int(settings.max_steps)
        self.limit_reached = False
        self.llm_seconds = 0.0
        self.tool_seconds = 0.0
        self.tokens = 0
        self.stream_started = 0.0
        self.stream_rate = 0.0
        self.answer_raw = ""
        self.answer_active = False
        self.plan: list[dict] = []
        self.plan_announced = False
        self.tool_stats: dict[str, ToolStat] = {}
        self.tool_calls = 0
        self.tool_ok = 0
        self.tool_fail = 0
        self.step_log: list[dict] = []
        self.step_durations: list[float] = []
        self.workers: dict[int, Worker] = {}
        self.worker_entries: dict[int, Entry] = {}
        self.turn_worker_ids: set[int] = set()
        self.recent: list[tuple[str, str, str]] = []
        self.current_tool: Entry | None = None
        self.last_tool: tuple[str, str, float] | None = None

        self.scope_badge = ""
        self.lessons_count = 0
        self.memory_count = 0
        self.skills_count = 0
        self.tool_count = 0

        self.scroll = 0
        self._scroll_max = 0
        self.input_mode = False
        self.prompt = "ты ❯ "
        self.input = ""
        self.cursor = 0
        self.history: list[str] = []
        self.hist_index = 0
        self.draft = ""
        self._hint = ""
        self._hint_until = 0.0
        self._ctrl_c_at = 0.0

    # ------------------------------------------------------------------ public
    @staticmethod
    def available() -> bool:
        if os.environ.get("ATLAS_NO_PANEL"):
            return False
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        cols, rows = _size()
        return cols >= 80 and rows >= 20

    def bind(self, agent) -> None:
        self.agent = agent
        self.max_steps = int(self.settings.max_steps)
        self.refresh_environment()

    def open(self) -> bool:
        if not self.force and not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        cols, rows = _size()
        if not self.force and (cols < 80 or rows < 20):
            return False
        self.cols, self.rows = cols, rows
        self.active = True
        self._out = sys.stdout
        if self.force:
            return True
        self.history = self._load_history()
        try:
            self._out.write("\033[?1049h\033[?25l\033[2J\033[H")
            self._out.flush()
        except (OSError, ValueError):
            self.active = False
            return False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._redraw_loop,
            args=(self._stop,),
            name="atlas-panel",
            daemon=True,
        )
        self._thread.start()
        return True

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.6)
        try:
            self._out.write("\033[?25h\033[?1049l")
            self._out.flush()
        except (OSError, ValueError):
            pass
        if self.session_log_path and any(
            entry.kind in ("user", "answer", "tool", "worker", "helper", "summary")
            for entry in self.entries
        ):
            try:
                self.last_export = self.export_markdown(
                    Path(self.session_log_path).with_suffix(".md")
                )
            except OSError:
                pass

    def set_auto(self, value: bool) -> None:
        self.auto = bool(value)
        self.invalidate()

    def invalidate(self) -> None:
        # Кэш карточек уже сброшен точечно; здесь достаточно разбудить отрисовку.
        return

    # --------------------------------------------------------------- environment
    def refresh_environment(self) -> None:
        try:
            scope = load_scope(self.settings.data_path)
            if scope.authorized and not scope.is_empty():
                self.scope_badge = _style("✓ авторизован", GREEN)
            elif scope.is_empty():
                self.scope_badge = _style("пуст", YELLOW)
            else:
                self.scope_badge = _style("не авторизован", YELLOW)
        except Exception:
            self.scope_badge = _style("неизвестен", DIM)
        try:
            self.lessons_count = Lessons(self.settings.data_path / "lessons.jsonl").count()
            self.memory_count = len(Memory(self.settings.data_path / "memory.jsonl").all())
            self.skills_count = len(Skills(self.settings.skills_path).list())
            self.tool_count = len(tool_specs())
        except Exception:
            pass
        self.invalidate()

    def refresh_mutable_counts(self, tool_name: str) -> None:
        try:
            if tool_name in ("lesson_save",):
                self.lessons_count = Lessons(self.settings.data_path / "lessons.jsonl").count()
            elif tool_name in ("memory_save", "memory_forget"):
                self.memory_count = len(Memory(self.settings.data_path / "memory.jsonl").all())
            elif tool_name in ("save_skill", "delete_skill"):
                self.skills_count = len(Skills(self.settings.skills_path).list())
        except Exception:
            pass

    # ------------------------------------------------------------------- entries
    def add_object(self, entry: Entry) -> None:
        with self.lock:
            self._append(entry)
            self.invalidate()

    def _append(self, entry: Entry) -> None:
        self.entries.append(entry)
        if len(self.entries) > 700:
            del self.entries[:120]
        self.scroll = 0

    def add_entry(self, kind: str, text: str = "", **meta) -> None:
        with self.lock:
            self._append(Entry(kind, str(text or ""), meta=meta))
            self.invalidate()

    def say(self, text: str = "", style: str = "") -> None:
        with self.lock:
            self._append(Entry("plain", str(text or ""), meta={"style": style}))
            self.invalidate()

    def markdown(self, text: str) -> None:
        with self.lock:
            self._append(Entry("plain_md", str(text or "")))
            self.invalidate()

    def clear_transcript(self) -> None:
        with self.lock:
            self.entries.clear()
            self.scroll = 0
            self._append(Entry("plain", "Диалог очищен.", meta={"style": DIM}))
            self.invalidate()

    def welcome(self) -> None:
        with self.lock:
            self._append(Entry("welcome", "", meta={}))
            self.invalidate()

    def begin_turn(self, task: str) -> None:
        with self.lock:
            task = str(task or "").strip()
            self.turns += 1
            self.turn_started = time.monotonic()
            self.last_turn_seconds = 0.0
            self.turn_finished = False
            self.limit_reached = False
            self.task = task
            self.step = 0
            self.llm_seconds = 0.0
            self.tool_seconds = 0.0
            self.tokens = 0
            self.stream_started = 0.0
            self.stream_rate = 0.0
            self.answer_raw = ""
            self.answer_active = False
            if self.plan and all(
                str(item.get("status")) == "done" for item in self.plan
            ):
                # Завершённый план не тащим в новую задачу; незавершённый
                # оставляем, чтобы продолжение работы было видно в панели.
                self.plan = []
                self.plan_announced = False
            self.tool_stats = {}
            self.tool_calls = 0
            self.tool_ok = 0
            self.tool_fail = 0
            self.step_log = []
            self.step_durations = []
            self.turn_worker_ids = set()
            self.current_tool = None
            self.last_tool = None
            self.working = True
            self.phase = "подготовка"
            self.scroll = 0
            if task:
                self._append(Entry("user", task))
            self.invalidate()

    # ------------------------------------------------------------------- input
    def read_line(self, prompt: str = "ты ❯ ") -> str:
        if not self.active:
            return input(prompt)
        with self.lock:
            self.input_mode = True
            self.prompt = prompt
            self.input = ""
            self.cursor = 0
            self.hist_index = len(self.history)
            self.draft = ""
            self.scroll = 0
            self._render_locked()
        while True:
            key = _read_key()
            with self.lock:
                if key == "enter":
                    text = self.input
                    self.input_mode = False
                    self.input = ""
                    self.cursor = 0
                    self.scroll = 0
                    if text.strip():
                        self._remember(text)
                    self._render_locked()
                    return text
                if key == "interrupt":
                    now = time.monotonic()
                    if not self.input and now - self._ctrl_c_at < 1.8:
                        self.input_mode = False
                        self._render_locked()
                        raise KeyboardInterrupt
                    self._ctrl_c_at = now
                    self.input = ""
                    self.cursor = 0
                    self.scroll = 0
                    self._set_hint("Ctrl+C ещё раз — выход")
                    self._render_locked()
                    continue
                if key == "eof":
                    self.input_mode = False
                    self._render_locked()
                    raise EOFError
                if key == "backspace":
                    if self.cursor > 0:
                        self.input = self.input[: self.cursor - 1] + self.input[self.cursor :]
                        self.cursor -= 1
                elif key == "delete":
                    if self.cursor < len(self.input):
                        self.input = self.input[: self.cursor] + self.input[self.cursor + 1 :]
                elif key == "left":
                    self.cursor = max(0, self.cursor - 1)
                elif key == "right":
                    self.cursor = min(len(self.input), self.cursor + 1)
                elif key == "home":
                    self.cursor = 0
                elif key == "end":
                    self.cursor = len(self.input)
                elif key == "up":
                    self._history_up()
                elif key == "down":
                    self._history_down()
                elif key in ("pgup", "pgdn"):
                    page = max(4, self.rows - 10)
                    if key == "pgup":
                        self.scroll = min(self._scroll_max, self.scroll + page)
                    else:
                        self.scroll = max(0, self.scroll - page)
                elif key == "tab":
                    self._complete()
                elif key == "clear":
                    self.entries.clear()
                    self.scroll = 0
                    self._append(Entry("plain", "Экран очищен.", meta={"style": DIM}))
                elif key == "kill-line":
                    self.input = self.input[self.cursor :]
                    self.cursor = 0
                elif key == "kill-after":
                    self.input = self.input[: self.cursor]
                elif key == "kill-word":
                    self._kill_word()
                elif key == "esc":
                    self.input = ""
                    self.cursor = 0
                    self.scroll = 0
                elif len(key) == 1 and key.isprintable():
                    self.input = self.input[: self.cursor] + key + self.input[self.cursor :]
                    self.cursor += 1
                    self.scroll = 0
                self._render_locked()

    def _history_up(self) -> None:
        if not self.history:
            return
        if self.hist_index == len(self.history):
            self.draft = self.input
        self.hist_index = max(0, self.hist_index - 1)
        self.input = self.history[self.hist_index]
        self.cursor = len(self.input)

    def _history_down(self) -> None:
        if not self.history:
            return
        if self.hist_index < len(self.history) - 1:
            self.hist_index += 1
            self.input = self.history[self.hist_index]
        else:
            self.hist_index = len(self.history)
            self.input = self.draft
        self.cursor = len(self.input)

    def _kill_word(self) -> None:
        before = self.input[: self.cursor].rstrip()
        cut = max(before.rfind(" "), before.rfind("/"))
        if cut < 0:
            cut = 0
        self.input = self.input[:cut] + self.input[self.cursor :]
        self.cursor = cut

    def _remember(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            if len(self.history) > 200:
                del self.history[:50]
        try:
            path = self.settings.data_path / "repl_history.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(self.history[-200:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _load_history(self) -> list[str]:
        try:
            path = self.settings.data_path / "repl_history.txt"
            if not path.exists():
                return []
            return [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()][-200:]
        except OSError:
            return []

    def _set_hint(self, text: str, seconds: float = 2.5) -> None:
        self._hint = text
        self._hint_until = time.monotonic() + seconds

    def _complete(self) -> None:
        before = self.input[: self.cursor]
        if not before.startswith("/"):
            return
        if " " in before:
            head, _, token = before.partition(" ")
            head = head.strip()
            options: list[str] = []
            if head == "/agent":
                options = [profile.id for profile in PROFILES]
            elif head in ("/use", "/skill"):
                try:
                    options = [skill.name for skill in Skills(self.settings.skills_path).list()]
                except Exception:
                    options = []
            if not options:
                return
            matches = [item for item in options if item.startswith(token)]
            if len(matches) == 1:
                rest = self.input[self.cursor :]
                self.input = f"{head} {matches[0]} {rest}"
                self.cursor = len(head) + 1 + len(matches[0]) + 1
            elif matches:
                self._set_hint("  ".join(matches[:8]), 3.0)
            return
        matches = [command for command in COMMANDS if command.startswith(before)]
        if len(matches) == 1:
            rest = self.input[self.cursor :]
            self.input = matches[0] + " " + rest
            self.cursor = len(matches[0]) + 1
        elif matches:
            self._set_hint("  ".join(matches[:10]), 3.0)

    # --------------------------------------------------------------- questions
    def sync_plan(self) -> None:
        """Подтягивает план из ctx агента, если панель пропустила событие."""
        ctx = getattr(self.agent, "ctx", None)
        plan = getattr(ctx, "plan", None)
        if not isinstance(plan, list) or not plan:
            return
        normalized: list[dict] = []
        for item in plan[:30]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            status = str(item.get("status") or "pending").lower()
            if status not in ("pending", "active", "done", "failed"):
                status = "pending"
            normalized.append(
                {
                    "text": text[:300],
                    "status": status,
                    "note": str(item.get("note") or "")[:200],
                }
            )
        if normalized and normalized != self.plan:
            with self.lock:
                self.plan = normalized
                self.invalidate()

    def show_plan(self) -> None:
        if not self.plan:
            self.say("План не задан. Для сложной задачи агент составит его через plan_set.")
            return
        with self.lock:
            self._append(Entry("plan", "", meta={"items": [dict(item) for item in self.plan]}))
            self.invalidate()

    def show_stats(self) -> None:
        done = sum(1 for item in self.plan if item.get("status") == "done")
        elapsed = (
            time.monotonic() - self.turn_started
            if self.working and self.turn_started
            else self.last_turn_seconds
        )
        if self.workers:
            counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
            for worker in self.workers.values():
                counts[worker.status] = counts.get(worker.status, 0) + 1
            helpers_line = (
                f"  помощники: {len(self.workers)} · работают {counts['running']} · "
                f"в очереди {counts['queued']} · готово {counts['done']} · "
                f"ошибок {counts['failed']} · уроки: {self.lessons_count} · "
                f"память: {self.memory_count}"
            )
        else:
            helpers_line = (
                f"  помощники: 0 · уроки: {self.lessons_count} · "
                f"память: {self.memory_count}"
            )
        lines = [
            "Статистика сессии",
            f"  ходов: {self.turns} · сессия: {_seconds(time.monotonic() - self.session_started)}",
            f"  шаг: {self.step}/{self.max_steps} · текущий ход: {_seconds(elapsed)}",
            f"  LLM: {_seconds(self.llm_seconds)} · инструменты: {_seconds(self.tool_seconds)}",
            f"  вызовов: {self.tool_calls} · ок: {self.tool_ok} · ошибок: {self.tool_fail}",
            f"  символов ответа: {self.tokens} · {self.stream_rate:.0f} симв/с",
            f"  план: {done}/{len(self.plan)}" if self.plan else "  план: не задан",
            helpers_line,
        ]
        self.say("\n".join(lines))

    def show_workers(self) -> None:
        if not self.workers:
            self.say("Помощники ещё не запускались.")
            return
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        for worker in self.workers.values():
            counts[worker.status] = counts.get(worker.status, 0) + 1
        lines = [
            f"Помощники: всего {len(self.workers)} · работают {counts['running']} · "
            f"в очереди {counts['queued']} · готово {counts['done']} · "
            f"ошибок {counts['failed']}"
        ]
        for number, worker in sorted(self.workers.items()):
            status = {
                "queued": "в очереди",
                "running": "работает",
                "done": "готово",
                "failed": "ошибка",
            }.get(worker.status, worker.status)
            seconds = (
                time.monotonic() - worker.started
                if worker.status == "running" and worker.started
                else worker.seconds
            )
            timing = (
                "ждёт слот"
                if worker.status == "queued"
                else _seconds(seconds)
            )
            line = (
                f"  #{number} {status} · {timing} · "
                f"инструментов {worker.tools} · {worker.task or worker.last}"
            )
            if worker.status in ("queued", "running") and worker.phase:
                line += f"\n       сейчас: {worker.phase}"
            lines.append(line)
        self.say("\n".join(lines))

    def export_markdown(self, target: Path | None = None) -> Path:
        if target is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.settings.workspace_path / "reports" / f"chat-{stamp}.md"
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._markdown_transcript(), encoding="utf-8")
        self.last_export = target
        return target

    # ------------------------------------------------------------------ output
    def _redraw_loop(self, stop: threading.Event) -> None:
        while not stop.wait(0.12):
            with self.lock:
                if not self.active:
                    continue
                self.frame += 1
                self._render_locked()

    def _render_locked(self) -> None:
        if not self.active:
            return
        cols, rows = _size()
        self.cols, self.rows = cols, rows
        try:
            screen = self._screen(cols, rows)
        except Exception as exc:
            screen = [f"  ошибка панели: {type(exc).__name__}: {exc}"]
        self._draw(screen, cols)

    def _draw(self, screen: list[str], cols: int) -> None:
        try:
            parts: list[str] = ["\033[H"]
            for index, line in enumerate(screen):
                parts.append(_clip(line, max(1, cols - 1)))
                parts.append("\033[K")
                if index < len(screen) - 1:
                    parts.append("\r\n")
            self._out.write("".join(parts))
            self._out.flush()
        except (OSError, ValueError):
            self.active = False

    # ------------------------------------------------------------------- screen
    def _screen(self, cols: int, rows: int) -> list[str]:
        if cols < 60 or rows < 14:
            message = "  Терминал слишком мал для панели. Увеличь окно или задай ATLAS_NO_PANEL=1."
            return [_clip(message, max(1, cols - 1))] + [""] * max(0, rows - 1)

        content_h = max(6, rows - 5)
        sidebar_w = 38 if cols >= 112 else 34 if cols >= 96 else 30
        if cols < 90:
            sidebar_w = 0
        chat_w = cols - 1 if not sidebar_w else cols - sidebar_w - 2

        chat = self._chat(chat_w, content_h)
        header = self._header(cols)
        suffix = f" · ↑{self.scroll}" if self.scroll else ""
        chat_title = "ДИАЛОГ" + suffix
        chat_box = self._box(chat_title, chat, chat_w, content_h + 2, MAGENTA)

        if sidebar_w:
            side = self._sidebar(sidebar_w, content_h)
            side_box = self._box("ПАНЕЛЬ", side, sidebar_w, content_h + 2, CYAN)
            body = [side_box[index] + " " + chat_box[index] for index in range(len(side_box))]
        else:
            body = chat_box

        return [header] + body + [self._input_line(cols), self._footer(cols)]

    def _header(self, cols: int) -> str:
        gaps = [
            item
            for item in self.plan
            if str(item.get("status") or "pending") != "done"
        ]
        if self.working:
            status_text = self.phase
            status_color = YELLOW
        elif gaps and self.phase in ("готов", "готово", "завершено"):
            done = sum(1 for item in self.plan if item.get("status") == "done")
            status_text = f"план {done}/{len(self.plan)}"
            status_color = YELLOW
        else:
            status_text = self.phase
            status_color = GREEN
        segments = [
            _style(_G["mark"] + " " + self.settings.name + " " + __version__, "1;" + CYAN),
            _style("●", status_color) + " " + _style(status_text, status_color),
        ]
        if self.agent is not None:
            role = getattr(self.agent, "role_name", "") or "универсальный"
            segments.append(_style("агент: ", DIM) + _style(role, WHITE))
        segments.append(_style("модель: ", DIM) + _style(self.settings.llm.model, WHITE))
        segments.append(_style("скоуп: ", DIM) + (self.scope_badge or _style("—", DIM)))
        if self.workers:
            active = sum(
                1 for worker in self.workers.values() if worker.status == "running"
            )
            segments.append(
                _style("помощники: ", DIM)
                + _style(
                    f"{active}/{len(self.workers)}",
                    MAGENTA if active else GREEN,
                )
            )
        segments.append(_style("уроки: ", DIM) + _style(str(self.lessons_count), WHITE))
        segments.append(
            _style("авто: ", DIM)
            + (_style(_G["check"], GREEN) if self.auto else _style(_G["cross"], DIM))
        )
        segments.append(_style(datetime.now().strftime("%H:%M:%S"), DIM))
        line = ""
        separator = _style("  │  ", DIM)
        for segment in segments:
            candidate = segment if not line else line + separator + segment
            if _vlen(candidate) <= cols - 1:
                line = candidate
        return _pad(line, cols - 1)

    def _footer(self, cols: int) -> str:
        if self._hint and time.monotonic() < self._hint_until:
            text = "  " + self._hint
        elif self.working:
            text = (
                "  [Ctrl+C] прервать · панель обновляется · "
                "PgUp/PgDn — прокрутка после ответа"
            )
        else:
            text = (
                "  [Enter] отправить · [↑/↓] история · [PgUp/PgDn] прокрутка · "
                "[Tab] команды · [Ctrl+L] очистить · [Ctrl+C] выход"
            )
        return _pad(_style(text, DIM), cols - 1)

    def _input_line(self, cols: int) -> str:
        width = max(1, cols - 1)
        if self.input_mode:
            before = self.input[: self.cursor]
            after = self.input[self.cursor :]
            cursor = _style(after[0], REVERSE) if after else _style(" ", REVERSE)
            line = _style(self.prompt, "1;" + CYAN) + before + cursor + after[1:]
        else:
            spin = _SPIN[self.frame % len(_SPIN)] if self.working else "●"
            gaps = any(
                str(item.get("status") or "pending") != "done" for item in self.plan
            )
            color = (
                GREEN
                if not self.working
                and not gaps
                and self.phase in ("готов", "готово")
                else YELLOW
            )
            line = "  " + _style(spin, color) + " " + _style(self.phase, color)
            if self.working and self.turn_started:
                line += _style(
                    f" · {_seconds(time.monotonic() - self.turn_started)} · "
                    f"шаг {self.step}/{self.max_steps}",
                    DIM,
                )
        return _pad(line, width)

    # --------------------------------------------------------------- chat pane
    def _chat(self, width: int, height: int) -> list[str]:
        inner = max(8, width - 2)
        lines: list[str] = []
        for entry in self.entries:
            block = self._entry_lines(entry, inner)
            if not block:
                continue
            if lines:
                lines.append("")
            lines.extend(block)
        if self.answer_raw.strip():
            if lines:
                lines.append("")
            lines.extend(self._stream_lines(inner))
        if self.working:
            if lines:
                lines.append("")
            spin = _SPIN[self.frame % len(_SPIN)]
            elapsed = (
                _seconds(time.monotonic() - self.turn_started)
                if self.turn_started
                else "0.0с"
            )
            extra = f" · шаг {self.step}/{self.max_steps}" if self.step else ""
            lines.append(
                "  "
                + _style(spin, YELLOW)
                + " "
                + _style(self.phase, YELLOW)
                + _style(f" · {elapsed}{extra}", DIM)
            )
        if not lines:
            lines = [_style("  Напиши задачу ниже — агент начнёт работу.", DIM)]
        self._scroll_max = max(0, len(lines) - height)
        self.scroll = max(0, min(self.scroll, self._scroll_max))
        if self.scroll:
            end = max(0, len(lines) - self.scroll)
            return lines[max(0, end - height) : end]
        return lines[-height:]

    def _stream_lines(self, width: int) -> list[str]:
        head = _style("ATLAS", "1;" + CYAN) + _style(" · печатает…", DIM)
        body_width = max(4, width - 2)
        body = _markdown_lines(self.answer_raw, body_width)
        if len(body) > 80:
            body = body[:60] + [_style("…", DIM)] + body[-18:]
        result = [head]
        for line in body:
            result.append(_style(_G["v"], CYAN) + (" " + line if line else ""))
        return result

    def _entry_lines(self, entry: Entry, width: int) -> list[str]:
        if entry.cache_width == width and entry.cache:
            return entry.cache
        lines = self._build_entry(entry, width)
        entry.cache_width = width
        entry.cache = lines
        return lines

    def _build_entry(self, entry: Entry, width: int) -> list[str]:
        kind = entry.kind
        stamp = datetime.fromtimestamp(entry.ts).strftime("%H:%M")
        if kind == "user":
            lines = [_style("ты", "1;" + MAGENTA) + _style(" · " + stamp, DIM)]
            for line in _wrap(entry.text, max(4, width - 2)):
                lines.append(_style(_G["v"], MAGENTA) + (" " + _style(line, WHITE) if line else ""))
            return lines

        if kind == "answer":
            lines = [_style("ATLAS", "1;" + CYAN) + _style(" · " + stamp, DIM)]
            for line in _markdown_lines(entry.text, max(4, width - 2)):
                lines.append(_style(_G["v"], CYAN) + (" " + line if line else ""))
            return lines

        if kind == "tool":
            meta = entry.meta
            name = str(meta.get("name") or "инструмент")
            status = str(meta.get("status") or "running")
            color = str(meta.get("color") or CYAN)
            if status == "running":
                label = "выполняется"
                color = YELLOW
            else:
                label = str(meta.get("label") or "ok")
            seconds = float(meta.get("seconds") or 0.0)
            head = (
                _style("╭─ ", color)
                + _style(name, "1;" + color)
                + _style(" · " + stamp, DIM)
            )
            if int(meta.get("repeat") or 1) > 1:
                head += _style(f" · повтор {meta.get('repeat')}", YELLOW)
            lines = [head]
            args_text = str(meta.get("args") or "").strip()
            if args_text and args_text != "{}":
                lines.append(
                    _style(_G["v"] + " ", DIM)
                    + _style(_clip(args_text.replace("\n", " "), max(4, width - 3)), DIM)
                )
            tail = _style("╰─ ", color) + _style(label, color)
            if status != "running":
                tail += _style(" · " + _seconds(seconds), DIM)
            lines.append(tail)
            preview_lines = meta.get("preview") or []
            for line in preview_lines:
                lines.append(_style("   " + _clip(str(line), max(4, width - 4)), DIM))
            total = int(meta.get("total") or 0)
            if total > len(preview_lines):
                lines.append(_style(f"   … скрыто строк: {total - len(preview_lines)}", DIM))
            return lines

        if kind in ("helper", "helper_end"):
            color = GREEN if kind == "helper_end" and not entry.meta.get("failed") else MAGENTA
            head = (
                _style(_G["gear"] + " ", color)
                + _style("помощники", "1;" + color)
                + _style(" · " + stamp, DIM)
            )
            lines = [head]
            for line in _wrap(entry.text, max(4, width - 3))[:4]:
                lines.append(_style(_G["v"] + " ", color) + _style(line, WHITE))
            return lines

        if kind == "worker":
            number = entry.meta.get("worker") or 0
            status = str(entry.meta.get("status") or "running")
            color, label = {
                "queued": (BLUE, "в очереди"),
                "running": (MAGENTA, "работает"),
                "done": (GREEN, "готово"),
                "failed": (RED, "ошибка"),
            }.get(status, (MAGENTA, status))
            seconds = float(entry.meta.get("seconds") or 0.0)
            head = (
                _style(_G["bullet"], color)
                + _style(f" помощник #{number}", "1;" + color)
                + _style(f" · {label}", color)
            )
            if seconds:
                head += _style(f" · {_seconds(seconds)}", DIM)
            lines = [head]
            task = str(entry.meta.get("task") or "")
            if task:
                for line in _wrap(task, max(4, width - 4))[:2]:
                    lines.append(_style("   " + line, WHITE))
            if status in ("queued", "running"):
                brief = str(entry.meta.get("brief") or "")
                if brief:
                    for line in _wrap("бриф: " + brief, max(4, width - 4))[:2]:
                        lines.append(
                            _style("   " + _clip(line, max(4, width - 4)), DIM)
                        )
                phase = str(entry.meta.get("phase") or "")
                if status == "running" and phase:
                    lines.append(
                        _style("   " + _clip(phase, max(4, width - 4)), DIM)
                    )
            else:
                output = str(entry.meta.get("output") or "")
                for line in _preview_lines(output, 3):
                    lines.append(
                        _style("   " + _clip(line, max(4, width - 4)), DIM)
                    )
            return lines

        if kind == "plan":
            items = entry.meta.get("items") or []
            done = sum(1 for item in items if item.get("status") == "done")
            lines = [
                _style("ПЛАН", "1;" + CYAN)
                + _style(f" · {len(items)} шагов · выполнено {done}", DIM)
            ]
            for index, item in enumerate(items, 1):
                icon, color = _icon(item.get("status"))
                text = str(item.get("text") or "")
                lines.append(
                    _style(f" {icon} ", color)
                    + _style(str(index) + ". ", DIM)
                    + _style(_clip(text, max(4, width - 6)), WHITE)
                )
            return lines

        if kind == "notice":
            return [
                _style("! " + line, YELLOW)
                for line in _wrap(entry.text, max(4, width - 2))[:6]
            ]

        if kind == "summary":
            color = str(entry.meta.get("color") or DIM)
            label = _clip(entry.text, max(4, width - 4))
            return [_style("─ " + label + " " + _G["h"] * max(0, width - _vlen(label) - 3), color)]

        if kind == "welcome":
            return self._welcome_lines(width)

        if kind == "plain_md":
            return _markdown_lines(entry.text, width)

        if kind == "plain":
            style = str(entry.meta.get("style") or "")
            lines: list[str] = []
            for raw in _wrap(entry.text, max(4, width - 2)):
                text = _inline_md(raw) if not style else raw
                lines.append(_style(text, style) if style else text)
            return lines

        return _wrap(entry.text, width)

    def _welcome_lines(self, width: int) -> list[str]:
        lines: list[str] = []
        for line in _LOGO:
            lines.append(_style(line, "1;" + CYAN))
        lines.append("")
        lines.append(
            _style(self.settings.name + " " + __version__, "1;" + CYAN)
            + _style(" · локальный автономный агент", DIM)
        )
        lines.append(_style("  папка: ", DIM) + _style(str(self.settings.workspace_path), WHITE))
        lines.append(_style("  модель: ", DIM) + _style(self.settings.llm.model, WHITE))
        if self.agent is not None:
            role = getattr(self.agent, "role_name", "") or "универсальный"
            skill = getattr(self.agent, "skill", "")
            lines.append(_style("  агент: ", DIM) + _style(role, WHITE))
            if skill:
                lines.append(_style("  навык: ", DIM) + _style(skill, WHITE))
        lines.append(
            _style("  память: ", DIM)
            + _style(str(self.memory_count), WHITE)
            + _style(" · навыки: ", DIM)
            + _style(str(self.skills_count), WHITE)
            + _style(" · уроки: ", DIM)
            + _style(str(self.lessons_count), WHITE)
            + _style(" · инструменты: ", DIM)
            + _style(str(self.tool_count), WHITE)
        )
        lines.append("")
        lines.append(_style("  Советы", "1;" + YELLOW))
        for tip in (
            "Опиши задачу — агент сам составит план, и он появится слева.",
            "/help — команды · /plan — план · /worker — фоновый помощник · /stats — статистика сессии.",
            "PgUp/PgDn — прокрутка диалога · ↑/↓ — история ввода · Tab — автодополнение.",
            "Ctrl+C — прервать работу или (дважды) выйти · /export — сохранить диалог.",
        ):
            for index, chunk in enumerate(_wrap(tip, max(4, width - 6))):
                lines.append(
                    (_style("  " + _G["bullet"] + " ", CYAN) if index == 0 else "    ")
                    + _style(chunk, DIM)
                )
        return [_clip(line, width) for line in lines]

    # ---------------------------------------------------------------- sidebar
    def _sidebar(self, width: int, height: int) -> list[str]:
        inner = max(12, width - 2)
        out: list[str] = []

        def section(title: str, color: str = CYAN) -> str:
            return _style(" " + title, "1;" + color)

        def add(block: list[str], keep: int = 0) -> None:
            room = height - len(out)
            if room <= 0:
                return
            if len(block) <= room:
                out.extend(block)
            elif keep and room >= keep:
                out.extend(block[:room])

        role = getattr(self.agent, "role_name", "") if self.agent is not None else ""
        skill = getattr(self.agent, "skill", "") if self.agent is not None else ""
        elapsed = _seconds(time.monotonic() - self.session_started)
        identity = [
            f" {_style(_G['dot'], GREEN)} {_style(_clip(role or 'универсальный', inner - 4), '1;' + WHITE)}",
            f" {_style('модель', DIM)} {_clip(self.settings.llm.model, max(4, inner - 9))}",
        ]
        if skill:
            identity.append(f" {_style('навык', DIM)} {_clip(skill, max(4, inner - 8))}")
        identity.append(f" {_style('скоуп', DIM)} {self.scope_badge or '—'}")
        identity.append(
            f" {_style('сессия', DIM)} {elapsed} · {self.turns} ход(ов)"
            + (f" · {_style('авто', GREEN)}" if self.auto else "")
        )
        add(identity, 3)

        task_block = [section("ЗАДАЧА")]
        if self.task:
            for line in _wrap(self.task, max(4, inner - 3))[:2]:
                task_block.append(" " + _style(_G["arrow"], CYAN) + " " + _style(line, WHITE))
        else:
            task_block.append(_style("  нет активной задачи", DIM))
        add(task_block, 1)

        done = sum(1 for item in self.plan if item.get("status") == "done")
        failed = sum(1 for item in self.plan if item.get("status") == "failed")
        total = len(self.plan)
        title = f"ПЛАН {done}/{total}" if total else "ПЛАН"
        plan_block = [section(title, GREEN if total and done == total else CYAN)]
        if total:
            bar_width = max(6, min(16, inner - 14))
            plan_block.append(
                " "
                + _bar(done, total, bar_width, GREEN)
                + " "
                + _style(f"{int(done * 100 / total)}%", WHITE)
            )
            remaining = total - done
            if remaining:
                tail = f"осталось {remaining}"
                if failed:
                    tail += f" · ошибок {failed}"
                if done and self.turn_started:
                    eta = (time.monotonic() - self.turn_started) / done * remaining
                    tail += f" · ~{_seconds(eta)}"
                plan_block.append(" " + _style(tail, DIM))
            else:
                plan_block.append(" " + _style("все шаги выполнены", GREEN))
            room_items = max(2, height - len(out) - len(plan_block) - 4)
            show = min(total, room_items, 9)
            for index, item in enumerate(self.plan[:show], 1):
                status = str(item.get("status") or "pending")
                icon, color = _icon(status)
                text = _clip(str(item.get("text") or ""), max(4, inner - 4))
                if status == "done":
                    body = _style(text, DIM)
                elif status == "active":
                    body = _style(text, "1;" + YELLOW)
                elif status == "failed":
                    body = _style(text, RED)
                else:
                    body = _style(text, WHITE)
                plan_block.append(f" {_style(icon, color)} {_style(str(index) + '.', DIM)} {body}")
            if total > show:
                plan_block.append(_style(f"   {_G['ell']} ещё {total - show}", DIM))
        else:
            plan_block.append(_style("  агент не задал план", DIM))
            for step in self.step_log[-4:]:
                status = str(step.get("status") or "running")
                icon, color = _icon("active" if status == "running" else status)
                plan_block.append(
                    f" {_style(icon, color)} {_style(_clip(str(step.get('name') or ''), inner - 4), WHITE)}"
                )
        add(plan_block, 2)

        turn_elapsed = (
            time.monotonic() - self.turn_started
            if self.working and self.turn_started
            else self.last_turn_seconds
        )
        progress = [section("ПРОГРЕСС")]
        progress.append(f" шаг {self.step}/{self.max_steps} · {_seconds(turn_elapsed)}")
        progress.append(
            f" LLM {_seconds(self.llm_seconds)} · инстр. {_seconds(self.tool_seconds)}"
        )
        if self.tokens:
            progress.append(f" {self.tokens} симв. · {self.stream_rate:.0f} симв/с")
        active_workers = (
            sum(1 for worker in self.workers.values() if worker.status == "running")
            if self.workers
            else 0
        )
        workers_text = (
            f"{active_workers}/{len(self.workers)}" if self.workers else "0"
        )
        progress.append(
            f" инструментов {self.tool_calls} · помощников {workers_text}"
        )
        add(progress, 2)

        if self.workers:
            active = sum(1 for worker in self.workers.values() if worker.status == "running")
            queued = sum(1 for worker in self.workers.values() if worker.status == "queued")
            label = f"ПОМОЩНИКИ {active}/{len(self.workers)}"
            if queued:
                label += f" +{queued} в очереди"
            worker_block = [section(label, MAGENTA)]
            active_items = [
                (number, worker)
                for number, worker in sorted(self.workers.items())
                if worker.status in ("queued", "running")
            ]
            finished_items = [
                (number, worker)
                for number, worker in sorted(self.workers.items())
                if worker.status in ("done", "failed")
            ][-3:]
            hidden = len(self.workers) - len(active_items) - len(finished_items)
            for number, worker in sorted(active_items + finished_items, key=lambda item: item[0]):
                if worker.status == "running":
                    icon, color = _SPIN[self.frame % len(_SPIN)], YELLOW
                    status_text = "работает"
                    worker_seconds = time.monotonic() - worker.started
                elif worker.status == "queued":
                    icon, color = _G["dot"], BLUE
                    status_text = "в очереди"
                    worker_seconds = -1.0
                elif worker.status == "done":
                    icon, color = _G["check"], GREEN
                    status_text = "готово"
                    worker_seconds = worker.seconds
                else:
                    icon, color = _G["cross"], RED
                    status_text = "ошибка"
                    worker_seconds = worker.seconds
                worker_block.append(
                    f" {_style(icon, color)} {_style('#' + str(number), '1;' + MAGENTA)} "
                    f"{_style(status_text, color)}"
                    + (f" {_style(_seconds(worker_seconds), DIM)}" if worker_seconds >= 0 else "")
                    + f" · {worker.tools} инстр."
                )
                if worker.status in ("queued", "running") and worker.task:
                    worker_block.append(
                        _style("   " + _clip(worker.task, max(4, inner - 3)), DIM)
                    )
                if worker.status == "running" and worker.phase:
                    worker_block.append(
                        _style("   " + _clip(worker.phase, max(4, inner - 3)), DIM)
                    )
            if hidden > 0:
                worker_block.append(_style(f"   {_G['ell']} ещё {hidden}", DIM))
            add(worker_block, 2)

        if self.tool_stats:
            calls = sum(stat.calls for stat in self.tool_stats.values())
            tool_block = [
                section(f"ИНСТРУМЕНТЫ {calls}"),
                _style(
                    f"  {_G['check']} {self.tool_ok} · {_G['cross']} {self.tool_fail} · "
                    f"{_seconds(self.tool_seconds)}",
                    DIM,
                ),
            ]
            top = sorted(self.tool_stats.items(), key=lambda item: item[1].calls, reverse=True)[:3]
            max_calls = max((stat.calls for _, stat in top), default=0)
            for name, stat in top:
                label = _clip(name, 10)
                tool_block.append(
                    f" {_style(label + ' ' * max(0, 10 - _vlen(label)), WHITE)} "
                    f"{_mini_bar(stat.calls, max_calls, max(4, min(8, inner - 18)))} "
                    f"{stat.calls}"
                )
            add(tool_block, 0)

        activity = [section("АКТИВНОСТЬ", BLUE)]
        spark = _spark(self.step_durations[-16:])
        if spark:
            activity.append(
                f" {_style(spark, CYAN)}  {_style('шагов ' + str(len(self.step_durations)), DIM)}"
            )
        else:
            activity.append(_style("  пока тихо", DIM))
        for when, text, color in self.recent[-2:]:
            activity.append(
                f" {_style(when, DIM)} {_style(_clip(text, max(4, inner - 8)), color)}"
            )
        add(activity, 0)

        resources = [
            section("РЕСУРСЫ", BLUE),
            f" уроки {self.lessons_count} · память {self.memory_count}",
            f" навыки {self.skills_count} · инструменты {self.tool_count}",
        ]
        add(resources, 0)
        return out[:height]

    # ------------------------------------------------------------------- boxes
    @staticmethod
    def _box(title: str, content: list[str], width: int, height: int, accent: str) -> list[str]:
        width = max(8, width)
        height = max(3, height)
        inner = width - 2
        label = f" {title} " if title else ""
        label = _clip(label, max(0, inner - 2))
        fill = max(0, inner - _vlen(label) - 1)
        top = (
            _style(_G["tl"] + _G["h"], accent)
            + _style(label, "1;" + accent)
            + _style(_G["h"] * fill + _G["tr"], accent)
        )
        bottom = _style(_G["bl"] + _G["h"] * inner + _G["br"], accent)
        rows = [top]
        for index in range(height - 2):
            line = content[index] if index < len(content) else ""
            rows.append(
                _style(_G["v"], accent)
                + _pad(_clip(line, inner), inner)
                + _style(_G["v"], accent)
            )
        rows.append(bottom)
        return rows

    # ------------------------------------------------------------------ export
    def _markdown_transcript(self) -> str:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        role = getattr(self.agent, "role_name", "") if self.agent is not None else ""
        lines = [
            f"# {self.settings.name} — диалог {now}",
            "",
            f"- Модель: {self.settings.llm.model}",
            f"- Агент: {role or 'универсальный'}",
            f"- Папка: {self.settings.workspace_path}",
            f"- Сессия: {_seconds(time.monotonic() - self.session_started)} · ходов: {self.turns}",
        ]
        if self.plan:
            done = sum(1 for item in self.plan if item.get("status") == "done")
            lines.append(f"- План: {done}/{len(self.plan)}")
            for index, item in enumerate(self.plan, 1):
                mark = {"done": "x", "active": ">", "failed": "!", "pending": " "}.get(
                    str(item.get("status")), " "
                )
                lines.append(f"  {index}. [{mark}] {item.get('text', '')}")
        lines.extend(["", "---", ""])
        for entry in self.entries:
            if entry.kind == "welcome":
                continue
            if entry.kind == "user":
                lines.extend(["### ты", "", entry.text, ""])
            elif entry.kind == "answer":
                lines.extend(["### ATLAS", "", entry.text, ""])
            elif entry.kind == "tool":
                meta = entry.meta
                lines.append(f"### инструмент: {meta.get('name', '')}")
                lines.append("")
                if meta.get("args"):
                    lines.extend(["```", str(meta.get("args")), "```", ""])
                label = meta.get("label") or meta.get("status") or ""
                lines.append(f"Статус: {label} · {_seconds(float(meta.get('seconds') or 0))}")
                if meta.get("preview"):
                    lines.extend(["", "```", *[str(line) for line in meta["preview"]], "```"])
                lines.append("")
            elif entry.kind == "plan":
                continue
            elif entry.kind in ("helper", "helper_end"):
                lines.extend([f"_{entry.text.strip()}_", ""])
            elif entry.kind == "worker":
                meta = entry.meta
                number = meta.get("worker") or 0
                status = {
                    "queued": "в очереди",
                    "running": "работает",
                    "done": "готово",
                    "failed": "ошибка",
                }.get(str(meta.get("status") or ""), str(meta.get("status") or ""))
                lines.append(f"### помощник #{number} — {status}")
                lines.append("")
                if meta.get("task"):
                    lines.append(f"**Задача:** {meta.get('task')}")
                    lines.append("")
                if meta.get("brief"):
                    lines.append(f"**Пояснение:** {meta.get('brief')}")
                    lines.append("")
                seconds = float(meta.get("seconds") or 0.0)
                if seconds:
                    lines.append(f"_{_seconds(seconds)}_")
                    lines.append("")
                output = str(meta.get("output") or "").strip()
                if output:
                    lines.extend(["**Отчёт:**", "", "```", output, "```", ""])
            elif entry.kind == "notice":
                lines.extend([f"> {entry.text}", ""])
            elif entry.kind == "summary":
                lines.extend([f"--- {entry.text} ---", ""])
            else:
                if entry.text.strip():
                    lines.extend([entry.text, ""])
        return "\n".join(lines).rstrip() + "\n"

    # ------------------------------------------------------------------- demo
    def render_snapshot(self, cols: int = 120, rows: int = 36) -> str:
        with self.lock:
            self.cols, self.rows = cols, rows
            return "\n".join(self._screen(cols, rows))


# ---------------------------------------------------------------------------
# Ввод с клавиатуры
# ---------------------------------------------------------------------------


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {
                "H": "up",
                "P": "down",
                "K": "left",
                "M": "right",
                "G": "home",
                "O": "end",
                "I": "pgup",
                "Q": "pgdn",
                "S": "delete",
                "R": "insert",
            }.get(code, "")
        if char == "\r":
            return "enter"
        if char == "\n":
            return "enter"
        if char == "\x1b":
            return "esc"
        if char == "\x08":
            return "backspace"
        if char == "\x7f":
            return "backspace"
        if char == "\t":
            return "tab"
        if char == "\x03":
            return "interrupt"
        if char == "\x1a":
            return "eof"
        if char == "\x0c":
            return "clear"
        if char == "\x15":
            return "kill-line"
        if char == "\x0b":
            return "kill-after"
        if char == "\x17":
            return "kill-word"
        if char == "\x01":
            return "home"
        if char == "\x05":
            return "end"
        return char

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
        if char == "\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], 0.03)
            if not ready:
                return "esc"
            rest = sys.stdin.read(2)
            return {
                "[A": "up",
                "[B": "down",
                "[C": "right",
                "[D": "left",
                "[H": "home",
                "[F": "end",
                "[5": "pgup",
                "[6": "pgdn",
                "[3": "delete",
            }.get(rest, "esc")
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x7f":
            return "backspace"
        if char == "\x03":
            return "interrupt"
        if char == "\x04":
            return "eof"
        if char == "\t":
            return "tab"
        if char == "\x0c":
            return "clear"
        if char == "\x15":
            return "kill-line"
        if char == "\x0b":
            return "kill-after"
        if char == "\x17":
            return "kill-word"
        if char == "\x01":
            return "home"
        if char == "\x05":
            return "end"
        return char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Демонстрация панели без запуска модели: python -m agent.panel
# ---------------------------------------------------------------------------


def _demo() -> int:
    settings = Settings()
    panel = PanelSession(settings, auto=True, force=True)
    panel.refresh_environment()

    class DemoAgent:
        role_name = "Пентестер"
        skill = "full_auto_pentest"

    panel.bind(DemoAgent())
    panel.welcome()
    panel.begin_turn("проверь example.com: DNS, порты, TLS и заголовки, собери отчёт")
    panel.answer_raw = (
        "Проверяю **example.com** по плану.\n\n"
        "- DNS: A 93.184.216.34\n"
        "- Порты: 80, 443\n"
        "- TLS: сертификат валиден до 2027-01-01\n\n"
        "Дальше проверяю заголовки безопасности и собираю отчёт."
    )
    panel.answer_active = True
    panel.tokens = 212
    panel.stream_rate = 38.0
    panel.llm_seconds = 41.0
    panel.tool_seconds = 12.4
    panel.step = 5
    panel.plan = [
        {"text": "собрать DNS и WHOIS", "status": "done", "note": ""},
        {"text": "проверить порты 80/443", "status": "done", "note": ""},
        {"text": "проверить TLS и сертификат", "status": "active", "note": ""},
        {"text": "заголовки безопасности", "status": "pending", "note": ""},
        {"text": "найти админку и уязвимости", "status": "pending", "note": ""},
        {"text": "собрать отчёт в workspace/reports", "status": "pending", "note": ""},
    ]
    panel.step_log = [
        {"name": "dns_lookup", "status": "ok", "seconds": 1.2},
        {"name": "port_scan", "status": "ok", "seconds": 4.8},
        {"name": "tls_info", "status": "running", "seconds": 0.0},
    ]
    panel.step_durations = [8.0, 12.0, 6.0, 18.0, 9.0, 14.0, 7.0, 11.0]
    panel.tool_calls = 9
    panel.tool_ok = 8
    panel.tool_fail = 1
    panel.tool_stats = {
        "port_scan": ToolStat(calls=3, ok=3, seconds=7.2),
        "web_fetch": ToolStat(calls=2, ok=2, seconds=2.1),
        "dns_lookup": ToolStat(calls=1, ok=1, seconds=1.2),
    }
    panel.workers = {
        1: Worker(
            1,
            "сканирую 93.184.216.34",
            "running",
            tools=3,
            brief="проверить 22/80/443 и вернуть баннеры",
            phase="инструмент port_scan",
        ),
        2: Worker(
            2,
            "проверяю заголовки",
            "done",
            seconds=8.2,
            tools=2,
            brief="CSP, HSTS, X-Frame-Options",
            phase="готово",
        ),
        3: Worker(
            3,
            "подбираю админку",
            "queued",
            brief="только пути из wordlist, без брутфорса",
            phase="ждёт свободный слот",
        ),
    }
    panel.working = True
    panel.turn_started = time.monotonic() - 62.0
    panel.tool_seconds = 12.4
    panel.recent = [
        ("14:22", "dns_lookup · ok", GREEN),
        ("14:23", "port_scan · ok", GREEN),
        ("14:24", "tls_info · выполняется", YELLOW),
    ]
    print(panel.render_snapshot(122, 38))
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
