"""Полноэкранное меню ATLAS: дашборд, роли, навыки, инструменты, скоуп.

Меню работает в отдельном экране терминала (alternate screen) и управляется
стрелками/Enter. Если терминал не интерактивный, включается простой текстовый
режим, чтобы меню можно было использовать в скриптах и тестах.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from .knowledge import Memory, Skills
from .profiles import PROFILES, Profile
from .scope import load_scope
from .settings import Settings
from .tools import tool_specs

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_COLOR = not os.environ.get("NO_COLOR")

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

_UNICODE = "utf" in (getattr(sys.stdout, "encoding", "") or "").lower()
_G = (
    {
        "tl": "┌",
        "tr": "┐",
        "bl": "└",
        "br": "┘",
        "h": "─",
        "v": "│",
        "sel": "▸",
        "dot": "•",
        "ell": "…",
        "updown": "↑↓",
    }
    if _UNICODE
    else {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "h": "-",
        "v": "|",
        "sel": ">",
        "dot": "*",
        "ell": "...",
        "updown": "up/down",
    }
)

_LOGO = [
    " █████╗ ████████╗██╗      █████╗ ███████╗",
    "██╔══██╗╚══██╔══╝██║     ██╔══██╗██╔════╝",
    "███████║   ██║   ██║     ███████║███████╗",
    "██╔══██║   ██║   ██║     ██╔══██║╚════██║",
    "██║  ██║   ██║   ███████╗██║  ██║███████║",
    "╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚══════╝",
]


def c(text: str, code: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}{RESET}"


def vlen(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    plain = _ANSI_RE.sub("", text)
    if len(plain) <= width:
        return text
    if width <= len(_G["ell"]):
        return plain[:width]
    return plain[: width - len(_G["ell"])] + _G["ell"]


def fit(text: str, width: int, align: str = "left") -> str:
    text = clip(text, width)
    gap = max(0, width - vlen(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def wrap(text: str, width: int) -> list[str]:
    if width <= 1:
        return [text]
    if not str(text).strip():
        return [""]
    result: list[str] = []
    current = ""
    for word in str(text).replace("\t", "    ").split(" "):
        while len(word) > width:
            if current:
                result.append(current)
                current = ""
            result.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            result.append(current)
            current = word
    if current or not result:
        result.append(current)
    return result


def box(title: str, body: list[str], width: int, accent: str = CYAN) -> list[str]:
    width = max(4, width)
    inner = width - 2
    label = f" {title} " if title else ""
    if vlen(label) > inner - 2:
        label = clip(label, max(0, inner - 2))
    fill = max(0, inner - vlen(label) - 1)
    top = (
        c(_G["tl"] + _G["h"], accent)
        + c(label, "1;" + accent)
        + c(_G["h"] * fill + _G["tr"], accent)
    )
    rows = [top]
    for line in body:
        rows.append(c(_G["v"], accent) + fit(line, inner) + c(_G["v"], accent))
    rows.append(c(_G["bl"] + _G["h"] * inner + _G["br"], accent))
    return rows


def side_by_side(panels: list[list[str]], gap: int = 2) -> list[str]:
    heights = [len(panel) for panel in panels]
    widths = [max((vlen(line) for line in panel), default=0) for panel in panels]
    rows: list[str] = []
    for index in range(max(heights, default=0)):
        cells: list[str] = []
        for panel, width in zip(panels, widths):
            cell = panel[index] if index < len(panel) else ""
            cells.append(cell + " " * max(0, width - vlen(cell)))
        rows.append((" " * gap).join(cells))
    return rows


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(100, 30))
    return size.columns, size.lines


def _draw(lines: list[str]) -> None:
    columns, rows = _size()
    lines = lines[: max(1, rows - 1)]
    sys.stdout.write("\033[H" + "\n".join(lines) + "\033[0m\033[J")
    sys.stdout.flush()


class Terminal:
    """Альтернативный экран с восстановлением при выходе и ошибке."""

    def __init__(self) -> None:
        self.active = False

    def __enter__(self) -> "Terminal":
        self.active = bool(sys.stdout.isatty())
        if self.active:
            sys.stdout.write("\033[?1049h\033[?25l\033[2J")
            sys.stdout.flush()
        return self

    def suspend(self) -> None:
        if self.active:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()

    def resume(self) -> None:
        if self.active:
            sys.stdout.write("\033[?1049h\033[?25l\033[2J")
            sys.stdout.flush()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.active:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            self.active = False


def read_key() -> str:
    """Одна клавиша: enter, esc, up/down/pgup/pgdn/home/end или символ."""
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
            }.get(code, "")
        if char == "\r":
            return "enter"
        if char == "\x1b":
            return "esc"
        if char == "\x08":
            return "backspace"
        return char.lower() if char.isalpha() else char

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
        if char == "\x1b":
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
            }.get(rest, "esc")
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x7f":
            return "backspace"
        return char.lower() if char.isalpha() else char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


ACTIONS: tuple[tuple[str, str], ...] = (
    ("chat", "Чат с агентом"),
    ("task", "Разовая задача"),
    ("pentest", "Пентест / аудит"),
    ("agents", "Агенты и роли"),
    ("skills", "Навыки"),
    ("tools", "Инструменты"),
    ("memory", "Память"),
    ("parallel", "Параллельные задачи"),
    ("doctor", "Диагностика"),
    ("exit", "Выход"),
)


class MenuApp:
    def __init__(self, settings: Settings, auto: bool) -> None:
        self.settings = settings
        self.auto = auto
        self.index = 0
        self.skills = Skills(settings.skills_path)
        self.memory = Memory(settings.data_path / "memory.jsonl")
        self.term = Terminal()

    # ------------------------------------------------------------------ home
    def run(self) -> int:
        with Terminal() as term:
            self.term = term
            try:
                while True:
                    self.draw_home()
                    key = read_key()
                    if key in ("up", "k"):
                        self.index = (self.index - 1) % len(ACTIONS)
                    elif key in ("down", "j"):
                        self.index = (self.index + 1) % len(ACTIONS)
                    elif key in ("enter", " "):
                        result = self.activate(self.index)
                        if result is not None:
                            return result
                    elif key.isdigit():
                        number = int(key)
                        index = len(ACTIONS) - 1 if number == 0 else number - 1
                        if 0 <= index < len(ACTIONS):
                            self.index = index
                            result = self.activate(index)
                            if result is not None:
                                return result
                    elif key in ("q", "esc"):
                        return 0
            except KeyboardInterrupt:
                return 0

    def activate(self, index: int) -> int | None:
        action = ACTIONS[index][0]
        if action == "exit":
            return 0
        if action == "chat":
            self._chat()
        elif action == "task":
            self._task()
        elif action == "agents":
            self.agents_screen()
        elif action == "skills":
            self.skills_screen()
        elif action == "tools":
            self.tools_screen()
        elif action == "pentest":
            self.pentest_screen()
        elif action == "memory":
            self.memory_screen()
        elif action == "parallel":
            self.parallel_screen()
        elif action == "doctor":
            self._doctor()
        return None

    def _header_body(self, width: int, compact: bool) -> list[str]:
        scope = load_scope(self.settings.data_path)
        authorized = scope.authorized and not scope.is_empty()
        scope_text = c("АВТОРИЗОВАН", GREEN) if authorized else c("не авторизован", YELLOW)
        inner = width - 2
        body: list[str] = []
        if compact:
            body.append(
                fit(
                    c(f"{self.settings.name} {__version__}", "1;" + CYAN)
                    + c(" — локальный автономный агент", DIM),
                    inner,
                    "center",
                )
            )
        else:
            for index, line in enumerate(_LOGO):
                body.append(
                    fit(c(line, "1;" + (CYAN if index % 2 == 0 else BLUE)), inner, "center")
                )
            body.append(
                fit(
                    c(f"{self.settings.name} {__version__}", "1;" + CYAN)
                    + c(" — локальный автономный агент", DIM),
                    inner,
                    "center",
                )
            )
        body.append("")
        model = clip(self.settings.llm.model, max(8, inner // 2 - 12))
        left = f"{c('МОДЕЛЬ', DIM)}  {c(model, WHITE)}"
        right = f"{c('СКОУП', DIM)}  {scope_text}"
        left_width = max(20, inner // 2)
        body.append(fit(left, left_width) + fit(right, inner - left_width, "right"))

        tools_count = len(tool_specs())
        skills_count = len(self.skills.list())
        memory_count = len(self.memory.all())
        stats = "   ".join(
            [
                f"{c('ПАМЯТЬ', DIM)} {memory_count}",
                f"{c('НАВЫКИ', DIM)} {skills_count}",
                f"{c('АГЕНТЫ', DIM)} {len(PROFILES)}",
                f"{c('ИНСТРУМЕНТЫ', DIM)} {tools_count}",
            ]
        )
        body.append(fit(stats, inner, "center"))
        return body

    def _panel(self, title: str, rows: list[str], width: int, height: int) -> list[str]:
        body = list(rows)
        while len(body) < height:
            body.append("")
        return box(title, body, width)

    def draw_home(self) -> None:
        columns, rows = _size()
        width = min(max(66, columns - 4), 104)
        compact = width < 96 or rows < 29
        lines = box("", self._header_body(width, compact), width)
        height = len(ACTIONS)
        left_width = 30
        if compact:
            main = self._panel("ДЕЙСТВИЯ", self._action_rows(left_width), left_width, height)
        else:
            skills_width = 26
            agents_width = width - left_width - skills_width - 4
            skills_rows: list[str] = []
            all_skills = self.skills.list()
            for skill in all_skills[: height - 1]:
                skills_rows.append(f" {c(_G['dot'], GREEN)} {clip(skill.name, skills_width - 6)}")
            if len(all_skills) > height - 1:
                skills_rows.append(
                    c(f"   {_G['ell']} ещё {len(all_skills) - height + 1}", DIM)
                )
            if not all_skills:
                skills_rows.append(c("   (пока нет)", DIM))
            agents_rows: list[str] = []
            for profile in PROFILES[: height - 1]:
                agents_rows.append(f" {c(_G['dot'], MAGENTA)} {profile.name}")
            main = side_by_side(
                [
                    self._panel("ДЕЙСТВИЯ", self._action_rows(left_width), left_width, height),
                    self._panel("НАВЫКИ", skills_rows, skills_width, height),
                    self._panel("АГЕНТЫ", agents_rows, agents_width, height),
                ],
                gap=2,
            )
        lines += main
        hint = (
            f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  "
            f"{c('1-9,0', CYAN)} быстрый выбор  {c('q', CYAN)} выход"
        )
        lines += box("", [fit(hint, width - 2, "center")], width, DIM)
        _draw(lines)

    def _action_rows(self, width: int) -> list[str]:
        inner = width - 2
        rows = []
        for index, (_, title) in enumerate(ACTIONS):
            marker = _G["sel"] if index == self.index else " "
            number = (index + 1) % 10
            label = f" {marker} {number}  {title}"
            line = fit(label, inner)
            if index == self.index:
                line = c(line, REVERSE)
            rows.append(line)
        return rows

    # ---------------------------------------------------------------- launch
    def _external(self, func, pause: bool = True) -> None:
        self.term.suspend()
        try:
            func()
        except (KeyboardInterrupt, EOFError):
            pass
        except Exception as exc:  # меню не должно падать
            print(f"\n[ошибка] {type(exc).__name__}: {exc}")
        if pause:
            try:
                input("\nEnter — вернуться в меню...")
            except (EOFError, KeyboardInterrupt):
                pass
        self.term.resume()

    def _chat(self, profile: Profile | None = None, skill: str = "") -> None:
        from .cli import cmd_repl

        self._external(
            lambda: cmd_repl(self.settings, self.auto, profile=profile, skill=skill)
        )

    def _task(self, profile: Profile | None = None, skill: str = "") -> None:
        from .cli import cmd_ask

        def run() -> None:
            task = input("Задача: ").strip()
            if task:
                cmd_ask(self.settings, [task], self.auto, profile=profile, skill=skill)

        self._external(run)

    def _doctor(self) -> None:
        from .doctor import run_doctor

        self._external(lambda: run_doctor(self.settings))

    # ----------------------------------------------------------------- roles
    def agents_screen(self) -> None:
        index = 0
        while True:
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            body = [
                fit(c("Готовые роли: общие инструменты, разный стиль работы.", DIM), inner),
                "",
            ]
            for position, profile in enumerate(PROFILES):
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {profile.name} — {profile.tagline}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            frame = box("АГЕНТЫ И РОЛИ", body, width)
            hint = (
                f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  "
                f"{c('Esc', CYAN)} назад"
            )
            frame += box("", [fit(hint, inner, "center")], width, DIM)
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(PROFILES)
            elif key in ("down", "j"):
                index = (index + 1) % len(PROFILES)
            elif key == "enter":
                self.profile_screen(PROFILES[index])
            elif key in ("esc", "q"):
                return

    def profile_screen(self, profile: Profile) -> None:
        index = 0
        options = [
            ("chat", "Чат с этим агентом"),
            ("task", "Разовая задача"),
            ("skill", "Показать навык"),
            ("back", "Назад"),
        ]
        while True:
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            body = [
                fit(c(profile.tagline, DIM), inner),
                "",
                fit(f"{c('Навык', DIM)}  {profile.skill or 'без навыка'}", inner),
                "",
            ]
            for line in wrap(profile.instruction or "Универсальный режим без роли.", inner - 4):
                body.append("  " + line)
            body.append("")
            for position, (_, label) in enumerate(options):
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {label}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            frame = box(f"АГЕНТ: {profile.name}", body, width)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} выбрать  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j"):
                index = (index + 1) % len(options)
            elif key == "enter":
                action = options[index][0]
                if action == "back":
                    return
                if action == "chat":
                    self._chat(profile)
                elif action == "task":
                    self._task(profile)
                elif action == "skill":
                    if profile.skill and self.skills.get(profile.skill):
                        self.skill_view(profile.skill)
                    else:
                        self._external(
                            lambda: input("У этой роли нет навыка. Enter — назад...")
                        )
            elif key in ("esc", "q"):
                return

    # ---------------------------------------------------------------- skills
    def skills_screen(self) -> None:
        index = 0
        while True:
            entries = self.skills.list()
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            page = max(3, rows - 10)
            if entries:
                index = min(index, len(entries) - 1)
            offset = max(0, index - page + 1)
            body = [
                fit(c("Все методики ATLAS. Enter — открыть, t — сразу дать задачу.", DIM), inner),
                "",
            ]
            for position in range(offset, min(len(entries), offset + page)):
                skill = entries[position]
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {skill.name} — {skill.description}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            if not entries:
                body.append(c("  Навыков пока нет. Создайте через: run.cmd skill new ...", DIM))
            frame = box("НАВЫКИ", body, width)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  {c('t', CYAN)} задача  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k") and entries:
                index = (index - 1) % len(entries)
            elif key in ("down", "j") and entries:
                index = (index + 1) % len(entries)
            elif key == "enter" and entries:
                self.skill_view(entries[index].name)
            elif key == "t" and entries:
                self._task(skill=entries[index].name)
            elif key in ("esc", "q"):
                return

    def skill_view(self, name: str) -> None:
        found = self.skills.get(name)
        if not found:
            return
        meta, text = found
        while True:
            action = self._scroll_view(
                f"НАВЫК: {meta.name}",
                meta.description,
                text.splitlines(),
                action_key="t",
                action_label="задача с навыком",
            )
            if action != "t":
                return
            self._task(skill=meta.name)

    # ---------------------------------------------------------------- tools
    def tools_screen(self) -> None:
        specs = tool_specs()
        index = 0
        while True:
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            page = max(3, rows - 10)
            offset = max(0, index - page + 1)
            body = [
                fit(c("Инструменты, доступные агенту. Enter — подробно.", DIM), inner),
                "",
            ]
            for position in range(offset, min(len(specs), offset + page)):
                spec = specs[position]["function"]
                marker = _G["sel"] if position == index else " "
                description = str(spec.get("description") or "").split(".")[0]
                line = f" {marker} {spec['name']} — {description}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            frame = box("ИНСТРУМЕНТЫ", body, width)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(specs)
            elif key in ("down", "j"):
                index = (index + 1) % len(specs)
            elif key == "enter":
                spec = specs[index]
                import json

                lines = json.dumps(spec, ensure_ascii=False, indent=2).splitlines()
                self._scroll_view(
                    f"ИНСТРУМЕНТ: {spec['function']['name']}",
                    "",
                    lines,
                )
            elif key in ("esc", "q"):
                return

    # ---------------------------------------------------------------- memory
    def memory_screen(self) -> None:
        records = list(reversed(self.memory.recent(100)))
        index = 0
        while True:
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            page = max(3, rows - 10)
            if records:
                index = min(index, len(records) - 1)
            offset = max(0, index - page + 1)
            body = [
                fit(c("Долговременная память. Enter — прочитать запись.", DIM), inner),
                "",
            ]
            for position in range(offset, min(len(records), offset + page)):
                record = records[position]
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {record.get('id', '')}  {record.get('text', '')}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            if not records:
                body.append(c("  Память пуста. Факты сохраняются через memory_save или /teach.", DIM))
            frame = box("ПАМЯТЬ", body, width)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k") and records:
                index = (index - 1) % len(records)
            elif key in ("down", "j") and records:
                index = (index + 1) % len(records)
            elif key == "enter" and records:
                record = records[index]
                lines = [
                    f"id: {record.get('id', '')}",
                    f"дата: {record.get('ts', '')}",
                    f"теги: {', '.join(record.get('tags', [])) or '—'}",
                    "",
                    str(record.get("text", "")),
                ]
                self._scroll_view("ЗАПИСЬ ПАМЯТИ", "", lines)
            elif key in ("esc", "q"):
                return

    # -------------------------------------------------------------- pentest
    def pentest_screen(self) -> None:
        from .profiles import get_profile

        index = 0
        options = [
            ("pentester", "Пентест-чат — полный цикл по методике"),
            ("scout", "Разведка (recon) — DNS, WHOIS, TLS, порты"),
            ("auditor", "Аудит сайта — заголовки, TLS, технологии"),
            ("audit-task", "Быстрый аудит: указать цель"),
            ("scope", "Скоуп и авторизация"),
            ("tools", "Пентест-инструменты (что установлено)"),
            ("methodology", "Методика пентеста"),
            ("back", "Назад"),
        ]
        while True:
            scope = load_scope(self.settings.data_path)
            authorized = scope.authorized and not scope.is_empty()
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            status = (
                c("скоуп авторизован — публичные цели разрешены", GREEN)
                if authorized
                else c("скоуп не авторизован — только локальные/приватные цели", YELLOW)
            )
            body = [
                fit(c("Разведка, аудит и авторизованный пентест в одном месте.", DIM), inner),
                fit(status, inner),
                "",
            ]
            for position, (_, label) in enumerate(options):
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {label}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            frame = box("ПЕНТЕСТ / АУДИТ", body, width, RED)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} открыть  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j"):
                index = (index + 1) % len(options)
            elif key == "enter":
                action = options[index][0]
                if action == "back":
                    return
                if action in ("pentester", "scout", "auditor"):
                    self._chat(get_profile(action))
                elif action == "audit-task":
                    self._pentest_audit_task()
                elif action == "scope":
                    self.scope_screen()
                elif action == "tools":
                    self._pentest_tools_view()
                elif action == "methodology":
                    if self.skills.get("pentest-methodology"):
                        self.skill_view("pentest-methodology")
                    else:
                        self._external(
                            lambda: input("Навык 'pentest-methodology' не найден. Enter — назад...")
                        )
            elif key in ("esc", "q"):
                return

    def _pentest_audit_task(self) -> None:
        from .cli import cmd_ask
        from .profiles import get_profile

        def run() -> None:
            target = input("Цель (домен/URL/IP): ").strip()
            if not target:
                return
            task = f"сделай аудит {target} и собери отчёт по безопасности"
            cmd_ask(self.settings, [task], self.auto, profile=get_profile("auditor"))

        self._external(run)

    def _pentest_tools_view(self) -> None:
        from .tools.pentest import pentest_tools

        try:
            text = pentest_tools({}, None)
        except Exception as exc:  # pragma: no cover - защита меню
            text = f"[ошибка] {type(exc).__name__}: {exc}"
        self._scroll_view("ПЕНТЕСТ-ИНСТРУМЕНТЫ", "✓ установлен · ✗ нет в PATH", text.splitlines())

    # ---------------------------------------------------------------- scope
    def scope_screen(self) -> None:
        index = 0
        options = [
            ("show", "Показать скоуп"),
            ("add-host", "Добавить хост/домен"),
            ("add-net", "Добавить подсеть (CIDR)"),
            ("authorize", "Авторизовать скоуп"),
            ("deauthorize", "Снять авторизацию"),
            ("clear", "Очистить и деавторизовать"),
            ("back", "Назад"),
        ]
        while True:
            scope = load_scope(self.settings.data_path)
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            body = [fit(line, inner) for line in scope.summary().splitlines()]
            body.append("")
            for position, (_, label) in enumerate(options):
                marker = _G["sel"] if position == index else " "
                line = f" {marker} {label}"
                if position == index:
                    body.append(c(fit(line, inner), REVERSE))
                else:
                    body.append(fit(line, inner))
            frame = box("СКОУП ПЕНТЕСТА", body, width)
            frame += box(
                "",
                [fit(f"{c(_G['updown'], CYAN)} выбор  {c('Enter', CYAN)} выполнить  {c('Esc', CYAN)} назад", inner, "center")],
                width,
                DIM,
            )
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j"):
                index = (index + 1) % len(options)
            elif key == "enter":
                action = options[index][0]
                if action == "back":
                    return
                self._scope_action(action)
            elif key in ("esc", "q"):
                return

    def _scope_action(self, action: str) -> None:
        from .cli import cmd_scope

        namespace = argparse.Namespace

        def run() -> None:
            if action == "show":
                cmd_scope(self.settings, namespace(action="show"))
            elif action == "add-host":
                host = input("Хост/домен (можно *.example.com): ").strip()
                if host:
                    cmd_scope(self.settings, namespace(action="add-host", value=host))
            elif action == "add-net":
                net = input("Подсеть/IP (например, 192.168.56.0/24): ").strip()
                if net:
                    cmd_scope(self.settings, namespace(action="add-net", value=net))
            elif action == "authorize":
                print("Кто и на каком основании разрешает тестирование этих целей?")
                who = input("Авторизация: ").strip()
                if who:
                    cmd_scope(self.settings, namespace(action="authorize", text=[who]))
            elif action == "deauthorize":
                cmd_scope(self.settings, namespace(action="deauthorize"))
            elif action == "clear":
                cmd_scope(self.settings, namespace(action="clear"))

        self._external(run)

    # ------------------------------------------------------------- parallel
    def parallel_screen(self) -> None:
        from .cli import _make_agent

        def run() -> None:
            raw = input("Задачи через ';' или с новой строки: ").strip()
            tasks = [
                item.strip()
                for item in raw.replace(";", "\n").splitlines()
                if item.strip()
            ]
            if not tasks:
                return
            parent, _, renderer = _make_agent(self.settings, self.auto)
            parent.on_event = None
            parent.ctx.on_event = None
            workers = min(4, len(tasks))
            renderer.status.begin(
                f"параллельно: {len(tasks)} задач",
                estimate=max(30.0, 45.0 * ((len(tasks) + workers - 1) // workers)),
            )
            pairs = [(task, parent.spawn(task)) for task in tasks]

            def execute(pair):
                task, worker = pair
                try:
                    return task, True, worker.ask(task)
                except Exception as exc:
                    return task, False, f"{type(exc).__name__}: {exc}"

            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(execute, pairs))
            finally:
                renderer.status.end()

            print(f"\n{self.settings.name}: параллельный запуск {len(tasks)} подзадач\n")
            for task, ok, answer in results:
                mark = "OK" if ok else "ОШИБКА"
                print(f"### {task}\n[{mark}]\n{answer}\n")

        self._external(run)

    # ---------------------------------------------------------------- viewer
    def _scroll_view(
        self,
        title: str,
        subtitle: str,
        lines: list[str],
        action_key: str = "",
        action_label: str = "",
    ) -> str:
        offset = 0
        while True:
            columns, rows = _size()
            width = min(max(66, columns - 4), 104)
            inner = width - 2
            page = max(5, rows - 9)
            wrapped: list[str] = []
            for line in lines:
                wrapped.extend(wrap(line, inner - 4))
            if not wrapped:
                wrapped = [""]
            max_offset = max(0, len(wrapped) - page)
            offset = min(max(0, offset), max_offset)
            body = []
            if subtitle:
                body.append(fit(c(subtitle, DIM), inner))
                body.append("")
            body.extend("  " + line for line in wrapped[offset : offset + page])
            frame = box(title, body, width)
            hint = f"{c(_G['updown'], CYAN)} прокрутка  {c('PgUp/PgDn', CYAN)} страница  "
            if action_key:
                hint += f"{c(action_key, CYAN)} {action_label}  "
            hint += f"{c('Esc', CYAN)} назад   [{offset + 1}-{min(len(wrapped), offset + page)} из {len(wrapped)}]"
            frame += box("", [fit(hint, inner, "center")], width, DIM)
            _draw(frame)
            key = read_key()
            if key in ("up", "k"):
                offset -= 1
            elif key in ("down", "j"):
                offset += 1
            elif key == "pgup":
                offset -= page
            elif key == "pgdn":
                offset += page
            elif key == "home":
                offset = 0
            elif key == "end":
                offset = max_offset
            elif action_key and key == action_key:
                return key
            elif key in ("esc", "q"):
                return ""


def _plain_pentest_menu(settings: Settings, auto: bool) -> None:
    """Пентест-раздел в текстовом режиме (пайпы, тесты, старые терминалы)."""
    import argparse

    from .cli import cmd_ask, cmd_repl, cmd_scope
    from .profiles import get_profile
    from .tools.pentest import pentest_tools

    scope = load_scope(settings.data_path)
    status = "авторизован" if scope.authorized and not scope.is_empty() else "НЕ авторизован"
    print("\n--- Пентест / аудит ---")
    print(f"Скоуп: {status}")
    print("  [1] Пентест-чат (полный цикл)")
    print("  [2] Разведка / recon (scout)")
    print("  [3] Аудит сайта (auditor)")
    print("  [4] Быстрый аудит: указать цель")
    print("  [5] Скоуп: показать")
    print("  [6] Скоуп: авторизовать")
    print("  [7] Пентест-инструменты")
    print("  [0] Назад")
    try:
        choice = input("Выбор: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice == "1":
        cmd_repl(settings, auto, profile=get_profile("pentester"))
    elif choice == "2":
        cmd_repl(settings, auto, profile=get_profile("scout"))
    elif choice == "3":
        cmd_repl(settings, auto, profile=get_profile("auditor"))
    elif choice == "4":
        target = input("Цель (домен/URL/IP): ").strip()
        if target:
            cmd_ask(
                settings,
                [f"сделай аудит {target} и собери отчёт по безопасности"],
                auto,
                profile=get_profile("auditor"),
            )
    elif choice == "5":
        cmd_scope(settings, argparse.Namespace(action="show"))
    elif choice == "6":
        who = input("Кто и на каком основании разрешает тест: ").strip()
        if who:
            cmd_scope(settings, argparse.Namespace(action="authorize", text=[who]))
    elif choice == "7":
        print(pentest_tools({}, None))


def _plain_menu(settings: Settings, auto: bool) -> int:
    """Простое меню без ANSI — для пайпов, тестов и старых терминалов."""
    from .cli import cmd_ask, cmd_repl, run_doctor

    while True:
        scope = load_scope(settings.data_path)
        status = "АВТОРИЗОВАН" if scope.authorized and not scope.is_empty() else "не авторизован"
        skills = Skills(settings.skills_path).list()
        print()
        print("=" * 62)
        print(f"  {settings.name} {__version__} — локальный автономный агент")
        print("=" * 62)
        print(f"Модель: {settings.llm.model}   Скоуп: {status}")
        print(f"Навыки: {', '.join(item.name for item in skills) or '—'}")
        print(f"Агенты: {', '.join(item.id for item in PROFILES)}")
        print("-" * 62)
        print("  [1]  Чат с агентом")
        print("  [2]  Разовая задача")
        print("  [3]  Пентест / аудит (роли, скоуп, инструменты)")
        print("  [4]  Агенты и роли")
        print("  [5]  Навыки")
        print("  [6]  Инструменты")
        print("  [7]  Память")
        print("  [8]  Параллельные задачи")
        print("  [9]  Диагностика")
        print("  [0]  Выход")
        print("=" * 62)
        try:
            choice = input("Выбор: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("Пока.")
            return 0
        if choice in ("0", "q", "exit", ""):
            print("Пока.")
            return 0
        try:
            if choice == "1":
                cmd_repl(settings, auto)
            elif choice == "2":
                task = input("Задача: ").strip()
                if task:
                    cmd_ask(settings, [task], auto)
            elif choice == "3":
                _plain_pentest_menu(settings, auto)
            elif choice == "4":
                for profile in PROFILES:
                    print(f"  {profile.id:<10} {profile.name} — {profile.tagline}")
                identifier = input("Роль (Enter — назад): ").strip()
                if identifier:
                    found = next(
                        (p for p in PROFILES if p.id == identifier.lower()),
                        None,
                    )
                    if found:
                        action = input("1 — чат, 2 — задача: ").strip()
                        if action == "1":
                            cmd_repl(settings, auto, profile=found)
                        elif action == "2":
                            task = input("Задача: ").strip()
                            if task:
                                cmd_ask(settings, [task], auto, profile=found)
            elif choice == "5":
                for item in Skills(settings.skills_path).list():
                    print(f"  {item.name}: {item.description}")
            elif choice == "6":
                for spec in tool_specs():
                    function = spec["function"]
                    print(f"  {function['name']}: {function.get('description', '')[:90]}")
            elif choice == "7":
                memory = Memory(settings.data_path / "memory.jsonl")
                for record in memory.recent(20):
                    print(f"  {record['id']}  {record['text']}")
            elif choice == "8":
                raw = input("Задачи через ';': ").strip()
                tasks = [item.strip() for item in raw.split(";") if item.strip()]
                if tasks:
                    from .cli import _make_agent

                    parent, _, _ = _make_agent(settings, auto)
                    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
                        results = list(
                            pool.map(lambda task: (task, parent.run_subtask(task)), tasks)
                        )
                    for task, answer in results:
                        print(f"\n### {task}\n{answer}")
            elif choice == "9":
                run_doctor(settings)
            else:
                print("Нет такого пункта.")
        except Exception as exc:
            print(f"[ошибка] {type(exc).__name__}: {exc}")
        try:
            input("\nEnter — в меню...")
        except (EOFError, KeyboardInterrupt):
            return 0


def run_menu(settings: Settings, auto: bool) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _plain_menu(settings, auto)
    return MenuApp(settings, auto).run()
