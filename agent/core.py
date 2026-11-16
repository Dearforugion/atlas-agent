from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable

from .audit import AuditLog
from .context import Context
from .errors import LLMError, ToolError
from .knowledge import Memory, Skills
from .llm import LLM
from .scope import load_scope
from .settings import Settings
from .tools import execute_tool, tool_specs
from .utils import extract_text_tool_call, preview, truncate

TEXT_PROTOCOL = """## Текстовый протокол инструментов
Нативный вызов функций недоступен. Чтобы выполнить действие, верни РОВНО один блок:
<tool_call>{"name": "имя_инструмента", "arguments": {}}</tool_call>
Не добавляй пояснений до или после блока. После выполнения ты получишь <tool_result>.
Когда задача выполнена, ответь обычным текстом без блока <tool_call>."""

MAX_SUBAGENT_DEPTH = 1
"""Максимальная глубина вложенности воркеров-агентов: root -> subagent разрешено,
но subagent -> subagent запрещено (чтобы не было бесконечной цепочки агентов)."""


class Agent:
    def __init__(
        self,
        settings: Settings,
        confirm: Callable[[str], bool] | None = None,
        llm: LLM | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        depth: int = 0,
        role_name: str = "",
        role_text: str = "",
        skill: str = "",
    ) -> None:
        self.settings = settings
        self.confirm = confirm or (lambda description: False)
        self.llm = llm or LLM(settings.llm)
        self.on_event = on_event
        self.depth = depth
        self.role_name = str(role_name or "")
        self.role_text = str(role_text or "")
        self.skill = str(skill or "")
        self.memory = Memory(settings.data_path / "memory.jsonl")
        self.skills = Skills(settings.skills_path)
        self.audit = AuditLog(settings.data_path / "audit.log")
        self.ctx = Context(
            settings=settings,
            confirm=self.confirm,
            audit=self.audit,
            depth=self.depth,
            on_event=self.on_event,
        )
        self.history: list[dict] = []
        self._native = bool(settings.llm.native_tools)

    def reset(self) -> None:
        self.history.clear()

    def set_model(self, model: str) -> None:
        self.settings.llm.model = model
        self.llm.config.model = model

    def set_role(self, name: str, text: str, skill: str = "") -> None:
        self.role_name = str(name or "")
        self.role_text = str(text or "")
        self.skill = str(skill or "")

    def set_skill(self, name: str) -> None:
        self.skill = str(name or "")

    def spawn(self, task: str) -> "Agent":
        """Создаёт воркера-агента для подзадачи: своя история, но общий скоуп и безопасность."""
        from .errors import ToolError as _ToolError

        depth = self.depth + 1
        if depth > MAX_SUBAGENT_DEPTH:
            raise _ToolError(
                f"Достигнут лимит вложенности подзадач ATLAS (максимум {MAX_SUBAGENT_DEPTH} уровня). "
                f"Текущая глубина агента: {self.depth}. "
                "Вложенный запуск не выполняется — слишком длинная цепочка агентов."
            )
        agent = Agent(
            settings=self.settings,
            confirm=self.confirm,
            llm=self.llm,
            on_event=self.on_event,
            depth=depth,
            role_name=self.role_name,
            role_text=self.role_text,
            skill=self.skill,
        )
        agent.ctx = Context(
            settings=self.settings,
            confirm=self.confirm,
            audit=self.audit,
            session_id=self.ctx.session_id,
            depth=depth,
            on_event=self.on_event,
        )
        agent.history = []
        return agent

    def run_subtask(self, task: str) -> str:
        """Запускает одного воркера-агента на подзадачу и возвращает его ответ."""
        agent = self.spawn(task)
        return agent.ask(task)

    def ask(self, text: str) -> str:
        task = str(text).strip()
        if not task:
            raise ValueError("Пустой запрос.")
        started = time.monotonic()
        self.history.append({"role": "user", "content": task})
        try:
            answer = self._run_loop()
        except Exception:
            self.history.pop()
            raise
        finally:
            self._emit(
                "end",
                seconds=time.monotonic() - started,
                depth=self.depth,
            )
        self.history.append({"role": "assistant", "content": answer})
        self._trim_history()
        return answer

    def _trim_history(self, limit: int = 60) -> None:
        """Ограничивает историю, но всегда сохраняет исходную задачу (первое
        сообщение пользователя), чтобы агент не терял цель в длинном диалоге."""
        if len(self.history) <= limit:
            return
        first_user = next(
            (msg for msg in self.history if msg.get("role") == "user"), None
        )
        tail = self.history[-(limit - 1):]
        if first_user is not None and first_user not in tail:
            self.history = [first_user] + tail
        else:
            self.history = tail

    def _run_loop(self) -> str:
        messages = [{"role": "system", "content": self._system_prompt()}] + list(self.history)
        repeat_counts: dict[str, int] = {}
        last_text = ""
        self._emit(
            "start",
            model=self.settings.llm.model,
            native=self._native,
            depth=self.depth,
        )

        for step in range(1, self.settings.max_steps + 1):
            self._emit("step", step=step, depth=self.depth)
            started = time.monotonic()
            try:
                message = self.llm.chat(
                    messages,
                    tools=tool_specs() if self._native else None,
                )
            except LLMError as exc:
                if self._native and step == 1 and _looks_like_tools_error(exc):
                    self._native = False
                    messages[0]["content"] = self._system_prompt()
                    self._emit(
                        "notice",
                        text="Провайдер не поддерживает нативный вызов инструментов — включён текстовый протокол.",
                    )
                    message = self.llm.chat(messages, tools=None)
                else:
                    raise
            finally:
                self._emit(
                    "llm_done",
                    seconds=time.monotonic() - started,
                    depth=self.depth,
                )

            content = message.get("content") or ""
            if content:
                last_text = content.strip()
            calls = message.get("tool_calls") or []

            if calls:
                messages.append(
                    {"role": "assistant", "content": content, "tool_calls": calls}
                )
                for index, call in enumerate(calls):
                    call_id = call.get("id") or f"call_{step}_{index}"
                    name = "unknown"
                    try:
                        name, args = _parse_native_call(call)
                    except ToolError as exc:
                        result, ok = f"ОШИБКА: {exc}", False
                    else:
                        result, ok = self._execute(name, args, repeat_counts)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": result,
                        }
                    )
                continue

            # Некоторые модели пишут вызов инструмента текстом даже при native tools.
            raw_call = extract_text_tool_call(content)
            if raw_call:
                try:
                    payload = json.loads(raw_call)
                    if not isinstance(payload, dict):
                        raise ValueError
                    name = str(payload.get("name") or "")
                    args = payload.get("arguments") or payload.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    if not name:
                        raise ValueError("нет поля name")
                except (json.JSONDecodeError, ValueError, TypeError):
                    raw_call = None

            if raw_call:
                messages.append({"role": "assistant", "content": content})
                result, _ = self._execute(name, args, repeat_counts)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f'<tool_result name="{name}">\n{result}\n</tool_result>\n\n'
                            "Продолжай выполнение задачи. Когда закончишь — ответь обычным текстом "
                            "без блока <tool_call>."
                        ),
                    }
                )
                continue

            final = content.strip()
            if final:
                self._emit("answer", text=final, depth=self.depth)
                return final

            messages.append({"role": "assistant", "content": ""})
            messages.append(
                {
                    "role": "user",
                    "content": "Ты вернул пустой ответ. Продолжай выполнение или дай финальный ответ.",
                }
            )

        self._emit("limit", steps=self.settings.max_steps, depth=self.depth)
        tail = f"\nПоследний ответ модели: {last_text[:500]}" if last_text else ""
        return (
            f"Достигнут лимит {self.settings.max_steps} шагов, задача не завершена.{tail}\n"
            "Можно увеличить max_steps в config.json или разбить задачу на части."
        )

    def _execute(
        self,
        name: str,
        args: dict,
        repeat_counts: dict[str, int],
    ) -> tuple[str, bool]:
        key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        repeat_counts[key] = repeat_counts.get(key, 0) + 1
        count = repeat_counts[key]
        self._emit("tool_call", name=name, args=args, repeat=count, depth=self.depth)

        if count >= 4:
            result = (
                "ВНИМАНИЕ: это действие уже повторялось 3 раза и больше не выполняется. "
                "Проанализируй прошлые результаты, измени подход или дай финальный ответ."
            )
            self._emit("tool_result", name=name, ok=False, output=result, depth=self.depth)
            return result, False

        started = time.monotonic()
        try:
            result = execute_tool(name, args, self.ctx)
            ok = True
        except ToolError as exc:
            result, ok = f"ОШИБКА: {exc}", False
        except Exception as exc:  # инструменты не должны ронять агента
            result, ok = f"ОШИБКА ({type(exc).__name__}): {exc}", False

        result = truncate(str(result), self.settings.max_tool_output)
        if count >= 3:
            result = (
                "ВНИМАНИЕ: ты повторяешь одно и то же действие. "
                "Результат ниже, но смени подход, если он не помогает.\n\n" + result
            )
        self.audit.write(
            "tool_call",
            name=name,
            args=args,
            ok=ok,
            preview=preview(result, 300),
        )
        self._emit(
            "tool_result",
            name=name,
            ok=ok,
            output=result,
            seconds=time.monotonic() - started,
            depth=self.depth,
        )
        return result, ok

    def _system_prompt(self) -> str:
        settings = self.settings
        workspace = settings.workspace_path
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        skills = self.skills.list()
        skills_text = (
            "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)
            if skills
            else "- (пока нет)"
        )
        facts = self.memory.recent(settings.memory_context)
        facts_text = (
            "\n".join(f"- {record['text']}" for record in facts)
            if facts
            else "- (пусто)"
        )
        protocol = "" if self._native else "\n" + TEXT_PROTOCOL + "\n"
        extra = f"\n## Дополнительные указания\n{settings.system_extra}\n" if settings.system_extra else ""
        role_block = ""
        if self.role_text:
            role_title = self.role_name or "специализированный агент"
            role_block = f"\n## Роль: {role_title}\n{self.role_text}\n"
        skill_block = ""
        if self.skill:
            found = self.skills.get(self.skill)
            if found:
                skill_block = f"\n## Активный навык: {found[0].name}\n{found[1]}\n"

        scope = load_scope(settings.data_path)
        if scope.authorized and not scope.is_empty():
            scope_state = (
                f"Скоуп АВТОРИЗОВАН. Разрешённые цели — хосты: "
                f"{', '.join(scope.hosts) or '—'}; подсети: {', '.join(scope.cidrs) or '—'}."
            )
        elif scope.is_empty():
            scope_state = "Скоуп ПУСТ и не авторизован — пентест-инструменты заблокированы."
        else:
            scope_state = "Скоуп НЕ авторизован — пентест-инструменты заблокированы до авторизации человеком."
        if settings.allow_private_targets:
            scope_state += (
                " Лабораторный режим: локальные/приватные цели "
                "(10/8, 172.16/12, 192.168/16, loopback) разрешены без скоупа; "
                "публичные цели — только через скоуп."
            )
        lab_note = (
            "\n   Исключение: в лабораторном режиме локальные/приватные цели разрешены без скоупа."
            if settings.allow_private_targets
            else ""
        )

        return f"""Ты — {settings.name}, автономный ИИ-агент на компьютере пользователя.

## Обстановка
- ОС: Windows
- Рабочая папка: {workspace}
- Время: {now}
- Модель: {settings.llm.model}
- Лимит шагов на задачу: {settings.max_steps}

## Главный принцип
Ты исполнитель, а не советчик. Если задачу можно выполнить доступными инструментами — выполняй её сразу, не спрашивая разрешения на безопасные действия и не описывая, что собираешься сделать.
Работай до результата: создал файл — проверь его; написал код — запусти; нашёл информацию — перепроверь по второму источнику.

## Как думать и работать
1. Декомпозиция. В начале сложной задачи мысленно составь короткий план: цель, 2–5 шагов, какой инструмент на каждом и как поймёшь, что шаг удался. Для простой задачи план не нужен — действуй.
2. Один шаг — один осмысленный вызов. Не вызывай инструмент наугад: сформулируй, какой факт или результат ты хочешь получить, и выбери самый подходящий инструмент с точными аргументами.
3. Анализируй каждый результат. После вывода инструмента сделай вывод: что узнал, приблизило ли это к цели, что делать дальше. Ошибка или пустой результат — не повод повторять то же самое: измени аргументы, инструмент или подход.
4. Не зацикливайся. Если два разных подхода не дали результата — не долби третий раз одно и то же. Смени стратегию или честно скажи, что именно не получилось и почему.
5. Проверяй себя. Прежде чем дать финальный ответ, сверься с исходной задачей: все ли пункты закрыты, не выдумал ли ты факты, воспроизводим ли результат. Если можешь проверить результат инструментом — проверь.
6. Факты важнее догадок. Не утверждай то, чего не проверял. Лучше проверить ещё раз, чем ответить наугад.

## Правила
1. Все новые файлы создавай внутри рабочей папки. За её пределы не выходи без явной необходимости.
2. Не выдумывай вывод инструментов и результаты действий. Если не знаешь — проверь.
3. Для файлов используй файловые инструменты, для команд — run_shell, для расчётов и обработки данных — run_python.
4. Для PowerShell вызывай: powershell -NoProfile -Command "...". Помни про экранирование кавычек.
5. Перед похожей задачей проверь навыки ниже и при необходимости загрузи подходящий через load_skill.
6. Если пользователь сообщает устойчивый факт о себе, проекте или правилах — сохрани его через memory_save.
7. Не раскрывай в ответах и файлах пароли, ключи и токены.
8. Если задача действительно неоднозначна — задай один короткий уточняющий вопрос. Во всех остальных случаях действуй.
9. Отвечай на языке пользователя, кратко и по существу. В конце — что именно сделано и где лежит результат.
10. Опасные и необратимые действия система подтверждает сама. Не спрашивай в тексте разрешение на обычные операции.
11. Для аудита сайта используй site_audit и crawl_site, для DNS — dns_lookup, для WHOIS — whois_lookup,
    для портов — port_scan; для глубокого пентеста — pentest_run.

## Пентест-режим
Ты умеешь проводить авторизованное тестирование на проникновение (recon, перечисление,
поиск уязвимостей, отчёты) инструментами nmap, httpx, nuclei, ffuf, gobuster, subfinder,
sqlmap, masscan, feroxbuster, sslscan, wpscan и др. через инструмент pentest_run. Правила жёсткие:
1. Работай по целям из авторизованного скоупа. Текущий статус: {scope_state}
2. Перед запуском сверяйся со скоупом (scope_check). Цель вне скоупа — не запускай и не ищи обход;
   объясни человеку, что цель надо добавить в скоуп через `agent scope`, и что это делает он, не ты.{lab_note}
3. Ты НЕ управляешь скоупом сам: добавление целей и авторизацию делает только человек через CLI.
4. Не атакуй произвольные цели «из интернета». Нет разрешения — нет теста. Отказ скоупа — это норма, а не ошибка.
5. Для каждой цели иди по методике (навык pentest-methodology): сначала пассив/рекон, потом активное,
   интрузивное — только с подтверждением. Фиксируй находки и собирай отчёт в workspace/reports/.
6. Находки, доступы, учётные данные не публикуй наружу и не используй за пределами скоупа.
7. Если задач несколько (разные хосты, поддомены, IP из подсети) — разбивай на независимые
   подзадачи и запускай их параллельно через инструмент subagent (tasks: ["...", "..."]).
   Воркеры наследуют скоуп, инструменты и правила, но работают со своей историей — это ускоряет
   полный цикл аудита по нескольким целям. Не запускай воркера в воркера глубже разрешённого уровня.
8. Полнота: для задач на аудит/пентест не останавливайся после первой найденной проблемы.
   Проверяй все цели и этапы методики, собирай полный список findings. В финальном ответе и
   отчёте в workspace/reports/ перечисли все находки: severity, описание, шаги воспроизведения,
   вывод/уровень уверенности, рекомендация. Честно фиксируй, что не удалось проверить — не
   выдумывай и не сокращай список без оснований.

## Долговременная память
{facts_text}

## Навыки (методики)
{skills_text}
Используй load_skill, если задача похожа на один из навыков.
{role_block}{skill_block}{protocol}{extra}"""

    def _emit(self, kind: str, **data) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            pass


def _parse_native_call(call: dict) -> tuple[str, dict]:
    if not isinstance(call, dict):
        raise ToolError("Некорректный вызов инструмента.")
    function = call.get("function") or {}
    name = str(function.get("name") or "").strip()
    if not name:
        raise ToolError("В вызове инструмента нет имени.")
    raw_args = function.get("arguments")
    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            args = {}
        else:
            try:
                args = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolError(f"Аргументы инструмента {name} — невалидный JSON: {text[:500]}") from exc
    else:
        args = {}
    if not isinstance(args, dict):
        raise ToolError(f"Аргументы инструмента {name} должны быть объектом.")
    return name, args


def _looks_like_tools_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "tool" in text or "function" in text or "unsupported" in text