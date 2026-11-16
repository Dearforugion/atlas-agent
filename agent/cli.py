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
from .errors import LLMError
from .knowledge import Memory, Skills
from .metrics import (
    Metrics,
    default_llm_seconds,
    default_tool_seconds,
    format_seconds,
)
from .profiles import PROFILES, Profile, get_profile
from .scope import clean_host_pattern, load_scope, normalize_target
from .selftest import run_selftest
from .settings import ROOT, Settings, load_settings
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
    """Печатает события агента, ведёт статус и считает ETA."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = StatusLine()
        self.metrics = Metrics(settings.data_path / "metrics.json")
        self.step = 0
        self._root_started: float | None = None
        self._summary = ""

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

    def handle(self, kind: str, data: dict) -> None:
        depth = int(data.get("depth") or 0)
        prefix = "подагент: " if depth else ""
        if kind == "start":
            if depth == 0:
                self.step = 0
                self._root_started = time.monotonic()
                self._summary = ""
            self.status.begin("готовлюсь к работе")
        elif kind == "step":
            step_no = int(data.get("step") or 0)
            if depth == 0:
                self.step = step_no
            self.status.begin(
                f"{prefix}думаю, шаг {step_no}/{self.settings.max_steps}",
                self._llm_estimate(),
            )
        elif kind == "llm_done":
            self.metrics.observe(
                "llm",
                self.settings.llm.model,
                float(data.get("seconds") or 0),
            )
        elif kind == "tool_call":
            name = str(data.get("name") or "инструмент")
            args = json.dumps(data.get("args", {}), ensure_ascii=False, default=str)
            self.status.write(_style(f"  > {name} {preview(args, 150)}", "2"))
            self.status.begin(
                f"{prefix}выполняю {name}",
                self.metrics.average(
                    "tool", name, default=default_tool_seconds(name)
                ),
            )
        elif kind == "tool_result":
            name = str(data.get("name") or "инструмент")
            self.metrics.observe("tool", name, float(data.get("seconds") or 0))
            if data.get("ok"):
                self.status.write(
                    _style(f"  < ok: {preview(str(data.get('output', '')), 170)}", "2")
                )
            else:
                self.status.write(
                    _style(f"  < err: {preview(str(data.get('output', '')), 220)}", "31")
                )
        elif kind == "notice":
            self.status.write(_style(f"  ! {data.get('text', '')}", "33"))
            if self.step:
                self.status.begin(
                    f"{prefix}думаю, шаг {self.step}/{self.settings.max_steps}",
                    self._llm_estimate(),
                )
        elif kind == "limit":
            self.status.write(_style("  ! достигнут лимит шагов", "31"))
        elif kind == "answer":
            if depth == 0:
                self._finish_turn("готово")
                self.status.end()
        elif kind == "end":
            if depth == 0:
                self._finish_turn("остановлено")
                self.status.end()


def _make_agent(
    settings: Settings,
    auto: bool,
    profile: Profile | None = None,
    skill: str = "",
) -> tuple[Agent, dict, ConsoleRenderer]:
    state = {"auto": auto}
    renderer = ConsoleRenderer(settings)

    def confirm(description: str) -> bool:
        if state["auto"]:
            return True
        renderer.status.write(_style(f"\n  [!] {description}", "33"))
        try:
            answer = input("      Разрешить? [y/N] ").strip().lower()
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
  /skills            список навыков
  /skill <name>      показать текст навыка
  /use <name>        подключить навык к текущей сессии
  /teach <текст>     сохранить факт в долговременную память
  /scope             показать авторизованный скоуп пентеста
  /session           путь журнала текущей сессии
  /exit              выход"""


def cmd_repl(
    settings: Settings,
    auto: bool,
    profile: Profile | None = None,
    skill: str = "",
) -> int:
    active = profile
    current_skill = skill or (profile.skill if profile else "")
    agent, state, renderer = _make_agent(
        settings, auto, profile=profile, skill=current_skill
    )
    log = SessionLog(settings)
    log.write(
        {
            "type": "start",
            "mode": "repl",
            "model": settings.llm.model,
            "agent": active.id if active else "",
        }
    )
    memory = Memory(settings.data_path / "memory.jsonl")

    print(_style(f"{settings.name} {__version__} — локальный автономный агент", "1"))
    print(f"Рабочая папка: {settings.workspace_path}")
    print(f"Модель: {settings.llm.model}  ({settings.llm.base_url})")
    print(f"Память: {len(memory.all())} фактов | Навыки: {len(Skills(settings.skills_path).list())}")
    if active and active.instruction:
        print(_style(f"Агент: {active.name} — {active.tagline}", "36"))
    if current_skill:
        print(_style(f"Активный навык: {current_skill}", "36"))
    print("Напиши задачу или /help. Выход: /exit.")

    while True:
        try:
            line = input(_style("\nты > ", "36")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока.")
            return 0
        if not line:
            continue

        if line.startswith("/"):
            action, _, argument = line.partition(" ")
            action = action.lower()
            argument = argument.strip()
            if action in ("/exit", "/quit", "/q"):
                print("Пока.")
                return 0
            if action == "/help":
                print(_HELP)
            elif action == "/reset":
                agent.reset()
                print("Контекст очищен.")
            elif action == "/yes":
                state["auto"] = not state["auto"]
                print(f"Авто-подтверждение: {'ВКЛ' if state['auto'] else 'ВЫКЛ'}")
            elif action == "/agents":
                for item in PROFILES:
                    marker = "*" if active and item.id == active.id else " "
                    print(f" {marker} {item.id:<10} {item.name} — {item.tagline}")
            elif action == "/agent":
                found = get_profile(argument) if argument else None
                if not found:
                    print("Неизвестный агент. Список: /agents")
                else:
                    active = found
                    current_skill = found.skill
                    agent.set_role(found.name, found.instruction, found.skill)
                    print(f"Агент: {found.name} — {found.tagline}")
                    if current_skill:
                        print(f"Активный навык: {current_skill}")
            elif action == "/model":
                if argument:
                    agent.set_model(argument)
                    print(f"Модель: {argument}")
                else:
                    print(f"Текущая модель: {settings.llm.model}")
            elif action == "/memory":
                limit = int(argument) if argument.isdigit() else 12
                records = memory.recent(max(1, min(50, limit)))
                if not records:
                    print("Память пуста.")
                for record in records:
                    print(f"  {record['id']}  {record['text']}")
            elif action == "/skills":
                skills = Skills(settings.skills_path).list()
                if not skills:
                    print("Навыков пока нет.")
                for item in skills:
                    marker = "*" if item.name == current_skill else " "
                    print(f" {marker} {item.name}: {item.description}")
            elif action == "/skill":
                found = Skills(settings.skills_path).get(argument) if argument else None
                if not found:
                    print("Навык не найден. Список: /skills")
                else:
                    print(found[1])
                    print(f"\nПодключить к сессии: /use {found[0].name}")
            elif action == "/use":
                found = Skills(settings.skills_path).get(argument) if argument else None
                if not found:
                    print("Навык не найден. Список: /skills")
                else:
                    current_skill = found[0].name
                    agent.set_skill(current_skill)
                    print(f"Активный навык: {current_skill}")
            elif action == "/teach":
                if not argument:
                    print("Использование: /teach <факт>")
                else:
                    record = memory.add(argument, ["repl"])
                    print(f"Запомнено ({record['id']}).")
            elif action == "/scope":
                print(load_scope(settings.data_path).summary())
            elif action == "/session":
                print(str(log.path))
            else:
                print("Неизвестная команда. Список: /help")
            continue

        try:
            answer = agent.ask(line)
        except LLMError as exc:
            print(f"\nОшибка модели:\n{exc}", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print("\n[прервано]")
            continue
        except Exception as exc:
            print(f"\n[ошибка] {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        finally:
            renderer.status.end()
        print(f"\n{settings.name} > {answer}")
        summary = renderer.summary()
        if summary and renderer.interactive:
            print(_style(f"  {summary}", "2"))
        log.write({"type": "turn", "user": line, "assistant": answer})

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
                raw = json.loads(path.read_text(encoding="utf-8"))
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