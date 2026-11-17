from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .core import Agent
from .daemon import run_daemon
from .doctor import run_doctor
from .errors import LLMError, ToolError
from .knowledge import Lessons, Memory, Skills
from .metrics import (
    Metrics,
    default_llm_seconds,
    default_tool_seconds,
    format_seconds,
)
from .panel import PanelSession
from .profiles import PROFILES, Profile, get_profile
from .scope import clean_host_pattern, load_scope, normalize_target
from .selftest import run_selftest
from .settings import ROOT, Settings, load_settings
from .tools import execute_tool
from .tools.subagent import add_background_workers
from .utils import now_iso, preview, read_text, write_text

_COLOR = False


def _enable_ansi() -> None:
    global _COLOR
    if os.environ.get("NO_COLOR"):
        return
    if sys.stdout.isatty():
        if sys.platform == "win32":
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                pass
        _COLOR = True


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _unicode_ok() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


_INLINE_RE = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[[^\]\n]+\]\([^)\n]+\))"
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


def _exit_code(output: str) -> int | None:
    match = _EXIT_RE.search(output or "")
    return int(match.group(1)) if match else None


def _preview_lines(output: str, limit: int = 4) -> list[str]:
    """Выбирает начало, конец и важные строки вывода, сохраняя порядок."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
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


def _inline_md(text: str) -> str:
    """Лёгкий markdown для одной строки: код, жирный, курсив, ссылки."""

    def repl(match: re.Match) -> str:
        token = match.group(0)
        if token.startswith("`"):
            return _style(token[1:-1], "33")
        if token.startswith("**") or token.startswith("__"):
            return _style(token[2:-2], "1")
        if token.startswith("[") and "](" in token:
            label, _, url = token[1:-1].partition("](")
            return _style(label, "4") + " " + _style(f"({url})", "2")
        if token.startswith("*"):
            return _style(token[1:-1], "3")
        return token

    return _INLINE_RE.sub(repl, text)


class _MarkdownStream:
    """Построчный markdown-рендер для потока токенов.

    Строка выводится, когда завершена, либо когда стала совсем длинной —
    тогда текст не «залипает» в буфере на длинном абзаце.
    """

    MAX_PARTIAL = 140

    def __init__(self) -> None:
        self._buf = ""
        self._fence = False
        self._mid = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: list[str] = []
        while self._buf:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                out.append(self._render_line(line) + "\n")
                self._mid = False
                continue
            if len(self._buf) >= self.MAX_PARTIAL:
                out.append(self._render_partial(self._buf))
                self._buf = ""
                self._mid = True
            break
        return "".join(out)

    def flush(self) -> str:
        if not self._buf:
            return ""
        line, self._buf = self._buf, ""
        self._mid = False
        return self._render_line(line)

    def _render_line(self, line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("```"):
            self._fence = not self._fence
            return _style(line, "2")
        if self._fence:
            return _style(line, "36")
        if stripped and not re.search(r"[A-Za-zА-Яа-я0-9]", stripped) and re.fullmatch(r"[\s\-*_=]{3,}", stripped):
            return _style(("─" if _unicode_ok() else "-") * 46, "2")
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            return _style(_inline_md(heading.group(2)), "1;36")
        quote = re.match(r"^(\s*)>\s?(.*)$", line)
        if quote:
            bar = "│" if _unicode_ok() else "|"
            return f"{quote.group(1)}{_style(bar, '36')} {_inline_md(quote.group(2))}"
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            dot = "•" if _unicode_ok() else "-"
            return f"{bullet.group(1)}{_style(dot, '36')} {_inline_md(bullet.group(2))}"
        return _inline_md(line)

    def _render_partial(self, text: str) -> str:
        if self._fence:
            return _style(text, "36")
        if not self._mid:
            heading = re.match(r"^(#{1,6})\s+(.*)$", text)
            if heading:
                return _style(_inline_md(heading.group(2)), "1;36")
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", text)
            if bullet:
                dot = "•" if _unicode_ok() else "-"
                return f"{bullet.group(1)}{_style(dot, '36')} {_inline_md(bullet.group(2))}"
        return _inline_md(text)


class StatusLine:
    """Живой индикатор работы агента внизу консоли.

    Пока модель думает или выполняется инструмент, строка показывает
    текущую фазу и прошедшее время. В неинтерактивном режиме (вывод в
    файл или пайп) вместо анимации печатается одна строка на фазу.
    """

    FRAMES = "|/-\\"

    def __init__(self) -> None:
        self._stream = sys.stdout
        self._interactive = bool(self._stream.isatty()) and not os.environ.get(
            "ATLAS_NO_SPINNER"
        )
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._label = ""
        self._started = 0.0
        self._estimate = 0.0
        self._width = 0
        self._full, self._empty = ("█", "░") if _unicode_ok() else ("#", "-")

    @property
    def interactive(self) -> bool:
        return self._interactive

    def begin(self, label: str, estimate: float = 0.0) -> None:
        """Запускает новую фазу работы; estimate — ожидаемая длительность."""
        self.end()
        with self._lock:
            self._label = label
            self._started = time.monotonic()
            self._estimate = max(0.0, float(estimate or 0.0))
            if not self._interactive:
                line = f"  ... {label}"
                if self._estimate > 1.0:
                    line += f" (обычно ~{self._estimate:.0f}с)"
                self._stream.write(_style(line, "2") + "\n")
                self._stream.flush()
                return
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._spin,
                args=(stop,),
                name="atlas-status",
                daemon=True,
            )
            self._thread.start()

    def _status_text(self, frame: str, elapsed: float) -> str:
        elapsed_text = f"{elapsed:.1f}" if elapsed < 10 else f"{elapsed:.0f}"
        if self._estimate <= 1.0:
            return f"  {frame} {self._label} | {elapsed_text}с"
        ratio = elapsed / self._estimate
        filled = max(0, min(10, int(ratio * 10)))
        bar = self._full * filled + self._empty * (10 - filled)
        text = f"  {frame} {self._label} [{bar}] {elapsed_text}с"
        if ratio < 0.92:
            left = max(1.0, self._estimate - elapsed)
            text += f" · ост. ~{left:.0f}с"
        elif ratio < 1.35:
            text += " · почти готово"
        else:
            text += " · дольше обычного"
        return text

    def _spin(self, stop: threading.Event) -> None:
        index = 0
        while not stop.wait(0.12):
            with self._lock:
                if stop.is_set():
                    return
                elapsed = time.monotonic() - self._started
                frame = self.FRAMES[index % len(self.FRAMES)]
                index += 1
                text = self._status_text(frame, elapsed)
                pad = " " * max(0, self._width - len(text))
                self._stream.write("\r" + text + pad)
                self._stream.flush()
                self._width = len(text)

    def end(self) -> None:
        """Останавливает анимацию и стирает строку статуса."""
        with self._lock:
            stop, self._stop = self._stop, None
            thread, self._thread = self._thread, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        with self._lock:
            if self._width:
                self._stream.write("\r" + " " * self._width + "\r")
                self._stream.flush()
                self._width = 0

    def write(self, text: str) -> None:
        """Печатает строку, не ломая строку статуса."""
        self.end()
        with self._lock:
            self._stream.write(text + "\n")
            self._stream.flush()


class ConsoleRenderer:
    """Печатает события агента: поток ответа, карточки инструментов, статусы."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = StatusLine()
        self.metrics = Metrics(settings.data_path / "metrics.json")
        self.step = 0
        self._root_started: float | None = None
        self._summary = ""
        self._out = sys.stdout
        self._stream_open = False
        self._stream_buf = ""
        self._last_stream = ""
        self._md = _MarkdownStream()
        self._lock = threading.RLock()

    @property
    def interactive(self) -> bool:
        return self.status.interactive

    def _llm_estimate(self) -> float:
        return self.metrics.average(
            "llm",
            self.settings.llm.model,
            default=default_llm_seconds(self.settings.llm.base_url),
        )

    def _finish_turn(self, prefix: str) -> None:
        if self._root_started is None:
            return
        seconds = time.monotonic() - self._root_started
        self._summary = (
            f"{prefix} за {format_seconds(seconds)} · шагов: {self.step}"
        )
        self._root_started = None

    def summary(self) -> str:
        text = self._summary
        self._summary = ""
        return text

    def _write_raw(self, text: str) -> None:
        try:
            self._out.write(text)
            self._out.flush()
        except (OSError, ValueError):
            pass

    def _start_stream(self) -> None:
        if self._stream_open:
            return
        self.status.end()
        self._md = _MarkdownStream()
        self._stream_buf = ""
        self._write_raw("\n" + _style(f"{self.settings.name} > ", "1;36"))
        self._stream_open = True

    def _write_token(self, text: str) -> None:
        if not text:
            return
        self._start_stream()
        rendered = self._md.feed(text)
        if rendered:
            self._write_raw(rendered)
        self._stream_buf += text

    def _close_stream(self) -> None:
        if not self._stream_open:
            return
        tail = self._md.flush()
        if tail:
            self._write_raw(tail)
        self._write_raw("\n")
        self._stream_open = False
        self._last_stream = self._stream_buf
        self._stream_buf = ""

    def answer_visible(self, text: str) -> bool:
        return bool(self._last_stream) and text.strip() == self._last_stream.strip()

    @staticmethod
    def _worker_label(data: dict) -> str:
        worker = data.get("worker")
        return f"помощник {worker}" if worker not in (None, "") else "помощник"

    def _handle_tool_call(self, data: dict, depth: int) -> None:
        name = str(data.get("name") or "инструмент")
        args = json.dumps(data.get("args", {}), ensure_ascii=False, default=str)
        detail = preview(args.replace("\n", " "), 220)
        if depth == 0:
            width = 64
            line = "  ╭─ " + name + " " + "─" * max(3, width - len(name) - 5)
            self.status.write(_style(line, "36"))
            if detail.strip() and detail.strip() != "{}":
                self.status.write(_style(f"  │ {detail}", "2"))
        else:
            self.status.write(
                _style(f"  [{self._worker_label(data)}] {name} {detail}", "35")
            )
        if depth == 0 or self.interactive:
            self.status.begin(
                f"{'подагент: ' if depth else ''}выполняю {name}",
                self.metrics.average("tool", name, default=default_tool_seconds(name)),
            )

    def _handle_tool_result(self, data: dict, depth: int) -> None:
        name = str(data.get("name") or "инструмент")
        ok = bool(data.get("ok"))
        seconds = float(data.get("seconds") or 0)
        self.metrics.observe("tool", name, seconds)
        output = str(data.get("output") or "").replace("\r", "")
        code = _exit_code(output) if name in ("run_shell", "run_python") else None
        if not ok:
            color, label = "31", "ошибка"
        elif code not in (None, 0):
            color, label = "33", f"exit {code}"
        else:
            color, label = "32", "ok"
        lines = _preview_lines(output, 4)
        total = sum(1 for line in output.splitlines() if line.strip())
        if depth == 0:
            self.status.write(_style(f"  ╰─ {label} · {format_seconds(seconds)}", color))
            for line in lines:
                self.status.write(_style(f"     {preview(line, 170)}", "2"))
            if total > len(lines):
                self.status.write(_style(f"     … скрыто строк: {total - len(lines)}", "2"))
        else:
            flat = preview(" | ".join(lines), 150)
            self.status.write(
                _style(
                    f"  [{self._worker_label(data)}] {name}: {label} · "
                    f"{format_seconds(seconds)} · {flat}",
                    color,
                )
            )
            if self.interactive:
                self.status.begin(f"{self._worker_label(data)}: работает")

    def _handle_subagent(self, kind: str, data: dict) -> None:
        if kind == "subagent_start":
            count = int(data.get("count") or 0)
            self.status.write(
                _style(f"  ╭─ помощники: {count} задач(и) параллельно", "35")
            )
        elif kind == "subagent_task_queued":
            task = preview(str(data.get("task") or ""), 110)
            self.status.write(
                _style(f"  │ [{self._worker_label(data)}] в очереди: {task}", "34")
            )
        elif kind == "subagent_task_start":
            task = preview(str(data.get("task") or ""), 110)
            self.status.write(
                _style(f"  │ [{self._worker_label(data)}] старт: {task}", "35")
            )
            self.status.begin(f"{self._worker_label(data)}: работает")
        elif kind == "subagent_task_done":
            ok = bool(data.get("ok"))
            color = "32" if ok else "31"
            state = "готово" if ok else "ошибка"
            seconds = float(data.get("seconds") or 0)
            output = preview(str(data.get("output") or "").replace("\n", " "), 140)
            self.status.write(
                _style(
                    f"  │ [{self._worker_label(data)}] {state} · "
                    f"{format_seconds(seconds)} · {output}",
                    color,
                )
            )
        elif kind == "subagent_end":
            seconds = float(data.get("seconds") or 0)
            ok_count = int(data.get("ok") or 0)
            failed = int(data.get("failed") or 0)
            color = "32" if failed == 0 else "33"
            self.status.write(
                _style(
                    f"  ╰─ помощники: {ok_count} ок, {failed} с ошибкой · "
                    f"{format_seconds(seconds)}",
                    color,
                )
            )

    def handle(self, kind: str, data: dict) -> None:
        with self._lock:
            self._handle(kind, data)

    def _handle(self, kind: str, data: dict) -> None:
        depth = int(data.get("depth") or 0)
        prefix = "подагент: " if depth else ""
        if kind == "start":
            self._close_stream()
            if depth == 0:
                self._last_stream = ""
                self.step = 0
                self._root_started = time.monotonic()
                self._summary = ""
                self.status.begin("готовлюсь к работе")
            elif self.interactive:
                self.status.begin(f"{self._worker_label(data)}: подключился")
        elif kind == "step":
            self._close_stream()
            step_no = int(data.get("step") or 0)
            if depth == 0:
                self.step = step_no
                self.status.begin(
                    f"{prefix}думаю, шаг {step_no}/{self.settings.max_steps}",
                    self._llm_estimate(),
                )
        elif kind == "token":
            if depth == 0:
                self._write_token(str(data.get("text") or ""))
        elif kind == "llm_done":
            self._close_stream()
            self.metrics.observe(
                "llm",
                self.settings.llm.model,
                float(data.get("seconds") or 0),
            )
        elif kind == "tool_call":
            self._close_stream()
            self._handle_tool_call(data, depth)
        elif kind == "tool_result":
            self._close_stream()
            self._handle_tool_result(data, depth)
        elif kind.startswith("subagent_"):
            self._close_stream()
            self._handle_subagent(kind, data)
        elif kind == "notice":
            self._close_stream()
            self.status.write(_style(f"  ! {data.get('text', '')}", "33"))
            if self.step:
                self.status.begin(
                    f"{prefix}думаю, шаг {self.step}/{self.settings.max_steps}",
                    self._llm_estimate(),
                )
        elif kind == "limit":
            self._close_stream()
            self.status.write(_style("  ! достигнут лимит шагов", "31"))
        elif kind == "answer":
            self._close_stream()
            if depth == 0:
                self._finish_turn("готово")
                self.status.end()
        elif kind == "end":
            self._close_stream()
            if depth == 0:
                self._finish_turn("остановлено")
                self.status.end()


def _make_agent(
    settings: Settings,
    auto: bool,
    profile: Profile | None = None,
    skill: str = "",
    renderer=None,
    line_input=None,
) -> tuple[Agent, dict, ConsoleRenderer]:
    state = {"auto": auto}
    renderer = renderer or ConsoleRenderer(settings)
    prompt = line_input or input
    confirm_lock = threading.Lock()

    def confirm(description: str) -> bool:
        if state["auto"]:
            return True
        with confirm_lock:
            renderer.status.write(_style(f"\n  [!] {description}", "33"))
            try:
                answer = prompt("      Разрешить? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
        return answer in ("y", "yes", "да", "д")

    agent = Agent(
        settings,
        confirm=confirm,
        on_event=renderer.handle,
        role_name=profile.name if profile else "",
        role_text=profile.instruction if profile else "",
        skill=skill or (profile.skill if profile else ""),
    )
    return agent, state, renderer


class SessionLog:
    def __init__(self, settings: Settings) -> None:
        directory = settings.data_path / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def write(self, payload: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": now_iso(), **payload}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def cmd_ask(
    settings: Settings,
    text: str,
    auto: bool,
    profile: Profile | None = None,
    skill: str = "",
) -> int:
    agent, _, renderer = _make_agent(settings, auto, profile=profile, skill=skill)
    log = SessionLog(settings)
    log.write({"type": "start", "mode": "ask", "model": settings.llm.model})
    task = " ".join(text).strip()
    try:
        answer = agent.ask(task)
    except LLMError as exc:
        print(f"\nОшибка модели:\n{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nОшибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        renderer.status.end()
    if not renderer.answer_visible(answer):
        print(f"\n{answer}")
    summary = renderer.summary()
    if summary and renderer.interactive:
        print(_style(f"  {summary}", "2"))
    log.write({"type": "turn", "user": task, "assistant": answer})
    return 0


_HELP = """Команды:
  /help              эта справка
  /reset             забыть контекст текущего диалога
  /yes               включить/выключить авто-подтверждение опасных действий
  /agents            список готовых ролей-агентов
  /agent <id>        переключить роль (например, scout или pentester)
  /model <id>        сменить модель до конца сессии
  /memory [n]        последние факты из памяти
  /lessons [n]       последние уроки самообучения
  /skills            список навыков
  /skill <name>      показать текст навыка
  /use <name>        подключить навык к текущей сессии
  /teach <текст>     сохранить факт в долговременную память
  /scope             показать авторизованный скоуп пентеста
  /plan              план текущей задачи и прогресс
  /stats             статистика сессии и инструментов
  /worker <задача>   добавить фонового помощника на ходу (можно ; несколько)
  /workers           состояние фоновых помощников и их отчёты
  /export            сохранить диалог в workspace/reports
  /clear             очистить экран диалога (панель)
  /session           путь журнала текущей сессии
  /exit              выход

Панель: слева план, прогресс, помощники и инструменты; справа диалог.
PgUp/PgDn — прокрутка, ↑/↓ — история, Tab — автодополнение, Ctrl+L — очистка.
Отключить панель: set ATLAS_NO_PANEL=1"""


def cmd_repl(
    settings: Settings,
    auto: bool,
    profile: Profile | None = None,
    skill: str = "",
) -> int:
    active = profile
    current_skill = skill or (profile.skill if profile else "")
    log = SessionLog(settings)

    panel = PanelSession(settings, auto=auto) if PanelSession.available() else None
    if panel is not None and not panel.open():
        panel = None
    renderer = panel.renderer if panel is not None else ConsoleRenderer(settings)
    agent, state, renderer = _make_agent(
        settings,
        auto,
        profile=profile,
        skill=current_skill,
        renderer=renderer,
        line_input=panel.read_line if panel is not None else None,
    )
    if panel is not None:
        panel.session_log_path = log.path
        panel.bind(agent)
        panel.welcome()

    log.write(
        {
            "type": "start",
            "mode": "repl",
            "model": settings.llm.model,
            "agent": active.id if active else "",
        }
    )
    memory = Memory(settings.data_path / "memory.jsonl")
    lessons = Lessons(settings.data_path / "lessons.jsonl")

    def say(text: str = "", style: str = "") -> None:
        if panel is not None:
            panel.say(text, style)
        else:
            print(text)

    def read_command() -> str:
        if panel is not None:
            return panel.read_line()
        return input(_style("\nты > ", "36"))

    if panel is None:
        print(_style(f"{settings.name} {__version__} — локальный автономный агент", "1"))
        print(f"Рабочая папка: {settings.workspace_path}")
        print(f"Модель: {settings.llm.model}  ({settings.llm.base_url})")
        print(
            f"Память: {len(memory.all())} фактов | "
            f"Навыки: {len(Skills(settings.skills_path).list())} | "
            f"Уроки: {lessons.count()}"
        )
        if active and active.instruction:
            print(_style(f"Агент: {active.name} — {active.tagline}", "36"))
        if current_skill:
            print(_style(f"Активный навык: {current_skill}", "36"))
        print("Напиши задачу или /help. Выход: /exit.")

    try:
        while True:
            try:
                line = read_command().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            if line.startswith("/"):
                action, _, argument = line.partition(" ")
                action = action.lower()
                argument = argument.strip()
                if action in ("/exit", "/quit", "/q"):
                    break
                if action == "/help":
                    say(_HELP)
                elif action == "/reset":
                    agent.reset()
                    say("Контекст очищен.")
                elif action == "/yes":
                    state["auto"] = not state["auto"]
                    if panel is not None:
                        panel.set_auto(bool(state["auto"]))
                    say(f"Авто-подтверждение: {'ВКЛ' if state['auto'] else 'ВЫКЛ'}")
                elif action == "/agents":
                    lines = ["Агенты:"]
                    for item in PROFILES:
                        marker = "*" if active and item.id == active.id else " "
                        lines.append(f" {marker} {item.id:<10} {item.name} — {item.tagline}")
                    say("\n".join(lines))
                elif action == "/agent":
                    found = get_profile(argument) if argument else None
                    if not found:
                        say("Неизвестный агент. Список: /agents")
                    else:
                        active = found
                        current_skill = found.skill
                        agent.set_role(found.name, found.instruction, found.skill)
                        say(f"Агент: {found.name} — {found.tagline}")
                        if current_skill:
                            say(f"Активный навык: {current_skill}")
                elif action == "/model":
                    if argument:
                        agent.set_model(argument)
                        say(f"Модель: {argument}")
                    else:
                        say(f"Текущая модель: {settings.llm.model}")
                elif action == "/memory":
                    limit = int(argument) if argument.isdigit() else 12
                    records = memory.recent(max(1, min(50, limit)))
                    if not records:
                        say("Память пуста.")
                    else:
                        say("\n".join(f"  {record['id']}  {record['text']}" for record in records))
                elif action == "/lessons":
                    limit = int(argument) if argument.isdigit() else 10
                    records = lessons.recent(max(1, min(50, limit)))
                    if not records:
                        say("Уроков пока нет.")
                    else:
                        lines = []
                        for record in records:
                            lines.append(f"  {record['id']}  {record.get('problem', '')}")
                            lines.append(f"        -> {record.get('solution', '')}")
                        say("\n".join(lines))
                elif action == "/skills":
                    skills = Skills(settings.skills_path).list()
                    if not skills:
                        say("Навыков пока нет.")
                    else:
                        lines = ["Навыки:"]
                        for item in skills:
                            marker = "*" if item.name == current_skill else " "
                            lines.append(f" {marker} {item.name}: {item.description}")
                        say("\n".join(lines))
                elif action == "/skill":
                    found = Skills(settings.skills_path).get(argument) if argument else None
                    if not found:
                        say("Навык не найден. Список: /skills")
                    else:
                        if panel is not None:
                            panel.markdown(found[1])
                            say(f"Подключить к сессии: /use {found[0].name}")
                        else:
                            print(found[1])
                            print(f"\nПодключить к сессии: /use {found[0].name}")
                elif action == "/use":
                    found = Skills(settings.skills_path).get(argument) if argument else None
                    if not found:
                        say("Навык не найден. Список: /skills")
                    else:
                        current_skill = found[0].name
                        agent.set_skill(current_skill)
                        say(f"Активный навык: {current_skill}")
                elif action == "/teach":
                    if not argument:
                        say("Использование: /teach <факт>")
                    else:
                        record = memory.add(argument, ["repl"])
                        say(f"Запомнено ({record['id']}).")
                elif action == "/scope":
                    say(load_scope(settings.data_path).summary())
                    if panel is not None:
                        panel.refresh_environment()
                elif action == "/plan":
                    if panel is not None:
                        panel.show_plan()
                    else:
                        say("План показывает левая панель интерактивного чата.")
                elif action == "/stats":
                    if panel is not None:
                        panel.show_stats()
                    else:
                        say("Статистика доступна в интерактивной панели.")
                elif action == "/worker":
                    if not argument:
                        say(
                            "Использование: /worker <задача> [; задача2 ...]\n"
                            "Помощники работают в фоне: /workers — статус, /plan — план."
                        )
                    else:
                        tasks = [
                            part.strip()
                            for part in re.split(r"[;\n]+", argument)
                            if part.strip()
                        ]
                        try:
                            records = add_background_workers(agent.ctx, tasks)
                        except ToolError as exc:
                            say(f"Не удалось добавить помощников: {exc}", "31")
                        else:
                            ids = ", ".join(f"#{record.id}" for record in records)
                            say(
                                f"Добавлено помощников: {len(records)} ({ids}). "
                                "Работают в фоне — /workers покажет статус."
                            )
                            if panel is not None:
                                panel.phase = "помощники"
                elif action == "/workers":
                    if panel is not None:
                        panel.show_workers()
                    else:
                        try:
                            say(execute_tool("worker_status", {}, agent.ctx))
                        except ToolError as exc:
                            say(f"Не удалось получить статус: {exc}", "31")
                elif action == "/export":
                    if panel is not None:
                        try:
                            path = panel.export_markdown()
                            say(f"Диалог сохранён: {path}")
                        except OSError as exc:
                            say(f"Не удалось сохранить: {exc}", "31")
                    else:
                        say("Экспорт доступен в интерактивной панели.")
                elif action == "/clear":
                    if panel is not None:
                        panel.clear_transcript()
                    else:
                        say("Очистка экрана доступна в интерактивной панели.")
                elif action == "/session":
                    say(str(log.path))
                else:
                    say("Неизвестная команда. Список: /help")
                continue

            try:
                if panel is not None:
                    panel.begin_turn(line)
                answer = agent.ask(line)
            except LLMError as exc:
                say(f"Ошибка модели:\n{exc}", "31")
                continue
            except KeyboardInterrupt:
                say("[прервано]")
                continue
            except Exception as exc:
                say(f"[ошибка] {type(exc).__name__}: {exc}", "31")
                continue
            finally:
                renderer.status.end()
            if not renderer.answer_visible(answer):
                if panel is not None:
                    panel.say(f"{settings.name} > {answer}")
                else:
                    print(f"\n{settings.name} > {answer}")
            summary = renderer.summary()
            if summary and renderer.interactive and panel is None:
                print(_style(f"  {summary}", "2"))
            log.write({"type": "turn", "user": line, "assistant": answer})
    finally:
        if panel is not None:
            panel.close()

    print("\nПока.")
    if panel is not None and panel.last_export is not None:
        print(f"Диалог сохранён: {panel.last_export}")
    return 0


def cmd_memory(settings: Settings, args) -> int:
    memory = Memory(settings.data_path / "memory.jsonl")
    action = args.action
    if action == "add":
        text = " ".join(args.text).strip()
        tags = [tag.strip() for tag in (args.tags or "").split(",") if tag.strip()]
        record = memory.add(text, tags)
        print(f"Запомнено ({record['id']}): {record['text']}")
    elif action == "list":
        records = memory.recent(args.limit)
        if not records:
            print("Память пуста.")
        for record in records:
            print(f"{record['id']}  [{record.get('ts', '')}]  {record['text']}")
    elif action == "search":
        records = memory.search(" ".join(args.query), limit=args.limit)
        if not records:
            print("Ничего не найдено.")
        for record in records:
            print(f"{record['id']}  {record['text']}")
    elif action == "forget":
        print("Удалено." if memory.forget(args.id) else "Запись не найдена.")
    elif action == "clear":
        count = memory.clear()
        print(f"Память очищена (удалено записей: {count}).")
    return 0


def cmd_skill(settings: Settings, args) -> int:
    skills = Skills(settings.skills_path)
    if args.action == "list":
        entries = skills.list()
        if not entries:
            print("Навыков пока нет.")
        for skill in entries:
            print(f"{skill.name}: {skill.description}")
    elif args.action == "show":
        found = skills.get(args.name)
        if not found:
            print("Навык не найден.")
            return 1
        print(found[1])
    elif args.action == "rm":
        print("Удалён." if skills.delete(args.name) else "Навык не найден.")
    elif args.action == "new":
        content = args.body or ""
        if args.file:
            content = read_text(Path(args.file))
        if not content and not sys.stdin.isatty():
            content = sys.stdin.read()
        if not content.strip():
            print("Нет текста навыка. Используй --body, --file или передай текст через stdin.")
            return 1
        try:
            meta, path = skills.save(args.name, args.description, content, overwrite=args.overwrite)
        except FileExistsError as exc:
            print(str(exc))
            return 1
        print(f"Создан навык {meta.name}: {path}")
    return 0


def cmd_config(settings: Settings, args) -> int:
    path = ROOT / "config.json"
    if args.action == "show":
        print(json.dumps(settings.as_dict(), ensure_ascii=False, indent=2))
    elif args.action == "path":
        print(str(path))
    elif args.action == "set":
        raw: dict = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                print(f"config.json повреждён: {exc}")
                return 1
        node = raw
        parts = args.key.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = _coerce(args.value)
        write_text(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        print(f"Сохранено: {args.key} = {node[parts[-1]]!r}")
        print("Изменения применятся при следующем запуске (кроме модели в текущей сессии).")
    return 0


def cmd_menu(settings: Settings, auto: bool) -> int:
    """Главное меню: дашборд со всеми навыками, ролями и быстрыми действиями."""
    from .tui import run_menu

    return run_menu(settings, auto)


def cmd_scope(settings: Settings, args) -> int:
    scope = load_scope(settings.data_path)
    action = args.action

    if action == "show":
        print(scope.summary())
        if settings.allow_private_targets:
            print(
                "\nЛабораторный режим: локальные/приватные цели "
                "(10/8, 172.16/12, 192.168/16, loopback) разрешены без скоупа."
            )
        if scope.path:
            print(f"\nФайл: {scope.path}")
        return 0

    if action == "add-host":
        host = clean_host_pattern(args.value)
        if not host:
            print("Не удалось определить хост. Укажи домен, например example.com или *.example.com.")
            return 1
        if host not in scope.hosts:
            scope.hosts.append(host)
            scope.save()
        print(f"Хост в скоупе: {host}")
        return 0

    if action == "add-net":
        import ipaddress

        try:
            net = str(ipaddress.ip_network(args.value, strict=False))
        except ValueError as exc:
            print(f"Неверный CIDR/IP: {exc}")
            return 1
        if net not in scope.cidrs:
            scope.cidrs.append(net)
            scope.save()
        print(f"Подсеть в скоупе: {net}")
        return 0

    if action == "allow":
        host = normalize_target(args.value)
        who = " ".join(args.text).strip()
        if not host:
            print("Не удалось определить хост. Укажи домен или URL.")
            return 1
        if not who:
            print("Подтверди, кто и на каком основании разрешает тест:")
            print(f'  agent scope allow {host} "Иван, мой сайт"')
            return 1
        if host not in scope.hosts:
            scope.hosts.append(host)
        scope.authorized = True
        scope.acknowledged_by = who
        scope.acknowledged_at = now_iso()
        scope.save()
        print(f"Цель добавлена и скоуп авторизован: {host}")
        print(scope.summary())
        return 0

    if action == "rm-host":
        want = clean_host_pattern(args.value) or args.value.strip().lower()
        raw_want = args.value.strip().lower().rstrip(".")
        removed = [h for h in scope.hosts if clean_host_pattern(h) == want or h.lower() == raw_want]
        if removed:
            scope.hosts = [h for h in scope.hosts if h not in removed]
            scope.save()
            print("Убрано: " + ", ".join(removed))
        else:
            print("Такого хоста нет в скоупе.")
        return 0

    if action == "rm-net":
        import ipaddress

        try:
            net = str(ipaddress.ip_network(args.value, strict=False))
        except ValueError:
            net = args.value
        if net in scope.cidrs:
            scope.cidrs.remove(net)
            scope.save()
            print(f"Убрано: {net}")
        else:
            print("Такой подсети нет в скоупе.")
        return 0

    if action == "authorize":
        who = " ".join(args.text).strip()
        if not who:
            print("Укажи, кто и на каком основании авторизует, например:")
            print('  agent scope authorize "Иван, домашний лаб + HTB-машины"')
            return 1
        if scope.is_empty():
            print("Сначала добавь хотя бы одну цель (add-host / add-net), потом авторизуй.")
            return 1
        scope.authorized = True
        scope.acknowledged_by = who
        scope.acknowledged_at = now_iso()
        scope.save()
        print("Скоуп АВТОРИЗОВАН. Агент сможет запускать инструменты по этим целям.")
        print(scope.summary())
        return 0

    if action == "deauthorize":
        scope.authorized = False
        scope.save()
        print("Авторизация снята. Пентест-инструменты заблокированы до повторной авторизации.")
        return 0

    if action == "clear":
        scope.hosts.clear()
        scope.cidrs.clear()
        scope.authorized = False
        scope.save()
        print("Скоуп очищен и деавторизован.")
        return 0

    return 1


def cmd_teach(settings: Settings, text: str) -> int:
    record = Memory(settings.data_path / "memory.jsonl").add(" ".join(text).strip(), ["cli"])
    print(f"Запомнено ({record['id']}): {record['text']}")
    print("Агент увидит этот факт при следующем запуске.")
    return 0


def _coerce(value: str):
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value.strip()):
        return float(value)
    if value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description=f"{'ATLAS'} {__version__} — локальный автономный ИИ-агент.",
    )
    parser.add_argument("--yes", action="store_true", help="автоматически подтверждать опасные действия")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    ask = sub.add_parser("ask", help="выполнить одну задачу и выйти")
    ask.add_argument("text", nargs="+", help="текст задачи")
    ask.add_argument("--yes", action="store_true", default=argparse.SUPPRESS, help="без вопросов подтверждать опасные действия")
    ask.add_argument("--agent", default="", help="роль-агент: scout, pentester, auditor, researcher, coder, sysadmin")
    ask.add_argument("--skill", default="", help="подключить навык по имени")

    repl = sub.add_parser("repl", help="интерактивный режим")
    repl.add_argument("--yes", action="store_true", default=argparse.SUPPRESS)
    repl.add_argument("--agent", default="", help="стартовая роль-агент")
    repl.add_argument("--skill", default="", help="подключить навык по имени")

    menu = sub.add_parser("menu", help="главное меню: роли, навыки, инструменты (start.bat)")
    menu.add_argument("--yes", action="store_true", default=argparse.SUPPRESS)

    sub.add_parser("doctor", help="проверить окружение и связь с моделью")
    sub.add_parser("selftest", help="прогнать автотесты без модели")

    daemon = sub.add_parser("daemon", help="следить за папкой inbox/ и выполнять задачи")
    daemon.add_argument("--yes", action="store_true", default=argparse.SUPPRESS)
    daemon.add_argument("--interval", type=float, default=None, help="интервал опроса, секунды")
    daemon.add_argument("--once", action="store_true", help="обработать очередь и выйти")

    memory = sub.add_parser("memory", help="работа с долговременной памятью")
    memory_sub = memory.add_subparsers(dest="action", required=True)
    mem_add = memory_sub.add_parser("add")
    mem_add.add_argument("text", nargs="+")
    mem_add.add_argument("--tags", default="")
    mem_list = memory_sub.add_parser("list")
    mem_list.add_argument("--limit", type=int, default=20)
    mem_search = memory_sub.add_parser("search")
    mem_search.add_argument("query", nargs="+")
    mem_search.add_argument("--limit", type=int, default=5)
    mem_forget = memory_sub.add_parser("forget")
    mem_forget.add_argument("id")
    memory_sub.add_parser("clear")

    skill = sub.add_parser("skill", help="работа с навыками")
    skill_sub = skill.add_subparsers(dest="action", required=True)
    skill_sub.add_parser("list")
    skill_show = skill_sub.add_parser("show")
    skill_show.add_argument("name")
    skill_rm = skill_sub.add_parser("rm")
    skill_rm.add_argument("name")
    skill_new = skill_sub.add_parser("new")
    skill_new.add_argument("name")
    skill_new.add_argument("--description", required=True)
    skill_new.add_argument("--body", default="")
    skill_new.add_argument("--file", default="")
    skill_new.add_argument("--overwrite", action="store_true")

    config = sub.add_parser("config", help="просмотр и изменение config.json")
    config_sub = config.add_subparsers(dest="action", required=True)
    config_sub.add_parser("show")
    config_sub.add_parser("path")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key", help="например llm.model или confirm")
    config_set.add_argument("value")

    teach = sub.add_parser("teach", help="сохранить факт в память")
    teach.add_argument("text", nargs="+")

    scope = sub.add_parser("scope", help="управление авторизованным скоупом пентеста")
    scope_sub = scope.add_subparsers(dest="action", required=True)
    scope_sub.add_parser("show", help="показать текущий скоуп")
    sc_ah = scope_sub.add_parser("add-host", help="добавить домен/хост")
    sc_ah.add_argument("value")
    sc_an = scope_sub.add_parser("add-net", help="добавить подсеть/IP (CIDR)")
    sc_an.add_argument("value")
    sc_al = scope_sub.add_parser("allow", help="добавить хост и сразу авторизовать")
    sc_al.add_argument("value")
    sc_al.add_argument("text", nargs="+", help="кто и на каком основании")
    sc_rh = scope_sub.add_parser("rm-host", help="убрать домен/хост")
    sc_rh.add_argument("value")
    sc_rn = scope_sub.add_parser("rm-net", help="убрать подсеть/IP")
    sc_rn.add_argument("value")
    sc_au = scope_sub.add_parser("authorize", help="подтвердить право тестировать скоуп")
    sc_au.add_argument("text", nargs="+", help="кто и на каком основании")
    scope_sub.add_parser("deauthorize", help="снять авторизацию")
    scope_sub.add_parser("clear", help="очистить и деавторизовать скоуп")

    return parser


def main(argv: list[str] | None = None) -> int:
    _enable_ansi()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    auto = bool(getattr(args, "yes", False))
    profile = get_profile(getattr(args, "agent", "") or "")
    if getattr(args, "agent", "") and profile is None:
        print(f"Неизвестный агент: {args.agent}. Список ролей: /agents в чате.")
        return 2
    skill = str(getattr(args, "skill", "") or "")

    try:
        if args.cmd is None:
            return cmd_repl(settings, auto, profile=profile, skill=skill)
        if args.cmd == "ask":
            return cmd_ask(settings, args.text, auto, profile=profile, skill=skill)
        if args.cmd == "repl":
            return cmd_repl(settings, auto, profile=profile, skill=skill)
        if args.cmd == "menu":
            return cmd_menu(settings, auto)
        if args.cmd == "doctor":
            return run_doctor(settings)
        if args.cmd == "selftest":
            return run_selftest(settings)
        if args.cmd == "daemon":
            return run_daemon(settings, auto=auto, interval=args.interval, once=args.once)
        if args.cmd == "memory":
            return cmd_memory(settings, args)
        if args.cmd == "skill":
            return cmd_skill(settings, args)
        if args.cmd == "config":
            return cmd_config(settings, args)
        if args.cmd == "teach":
            return cmd_teach(settings, args.text)
        if args.cmd == "scope":
            return cmd_scope(settings, args)
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())