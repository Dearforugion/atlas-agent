from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path

from .audit import AuditLog
from .context import Context
from .core import Agent
from .errors import ToolError
from .knowledge import Lessons, Memory, Skills
from .settings import Settings
from .tools import TOOLS, execute_tool, tool_specs


class ScriptedLLM:
    """Фальшивая модель для проверки цикла без интернета."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.index = 0
        self.requests: list[list[dict]] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
    ) -> dict:
        self.requests.append(messages)
        if self.index >= len(self.script):
            return {"role": "assistant", "content": "Готово."}
        message = self.script[self.index]
        self.index += 1
        if on_token and message.get("content"):
            on_token(str(message["content"]))
        return message


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def run_selftest(settings: Settings) -> int:
    print("Автотест ATLAS\n" + "=" * 48)
    root = Path(tempfile.mkdtemp(prefix="atlas-selftest-"))
    test_settings = replace(
        settings,
        workspace=str(root / "workspace"),
        data_dir=str(root / "data"),
        confirm="never",
        allow_outside_workspace=False,
    )
    test_settings.ensure_dirs()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            checks.append((name, True, str(detail)))
        except Exception as exc:
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    def check_agent_loop():
        script = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _call("c1", "write_file", {"path": "selftest/привет.txt", "content": "привет от ATLAS"}),
                    _call("c2", "make_dir", {"path": "selftest/вложенная"}),
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _call("c3", "read_file", {"path": "selftest/привет.txt"}),
                ],
            },
            {"role": "assistant", "content": "Файл создан и прочитан."},
        ]
        agent = Agent(test_settings, confirm=lambda description: True, llm=ScriptedLLM(script))
        answer = agent.ask("Создай тестовый файл")
        target = test_settings.workspace_path / "selftest" / "привет.txt"
        assert target.exists(), "файл не создан"
        assert "привет от ATLAS" in target.read_text(encoding="utf-8"), "неверное содержимое"
        assert (test_settings.workspace_path / "selftest" / "вложенная").is_dir(), "папка не создана"
        assert "прочитан" in answer, "нет финального ответа"
        return f"2 инструмента, ответ: {answer[:60]}"

    check("Цикл агента и файловые инструменты", check_agent_loop)

    def check_jail():
        ctx = Context(test_settings, lambda description: False, AuditLog(test_settings.data_path / "audit.log"))
        try:
            execute_tool("read_file", {"path": "../../секрет.txt"}, ctx)
        except ToolError:
            return "выход за рабочую папку заблокирован"
        raise AssertionError("выход за рабочую папку НЕ заблокирован")

    check("Изоляция рабочей папки", check_jail)

    def check_memory():
        memory = Memory(test_settings.data_path / "memory.jsonl")
        record = memory.add("Пользователя зовут Тест", ["имя"])
        found = memory.search("как зовут пользователя")
        assert found and found[0]["id"] == record["id"], "поиск не нашёл запись"
        return "сохранение и поиск работают"

    check("Долговременная память", check_memory)

    def check_lessons():
        lessons = Lessons(test_settings.data_path / "lessons.jsonl")
        record, created = lessons.add(
            "ModuleNotFoundError: No module named 'requests'",
            "python -m pip install requests",
            ["python", "pip"],
        )
        assert created, "урок не создан"
        found = lessons.search("No module named requests")
        assert found and found[0]["id"] == record["id"], "поиск урока не сработал"
        updated, created_again = lessons.add(
            "ModuleNotFoundError: No module named 'requests'",
            "pip install requests",
            ["python"],
        )
        assert not created_again, "дубликат урока не обновлён"
        assert updated["id"] == record["id"], "обновлён другой урок"
        return "сохранение, поиск и обновление урока работают"

    check("Уроки самообучения", check_lessons)

    def check_archive():
        ctx = Context(
            test_settings,
            lambda description: True,
            AuditLog(test_settings.data_path / "audit.log"),
        )
        folder = test_settings.workspace_path / "selftest"
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "sample.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("папка/файл.txt", "архив ок")
        execute_tool(
            "extract_archive",
            {"path": "selftest/sample.zip", "dest": "selftest/unzipped"},
            ctx,
        )
        extracted = folder / "unzipped" / "папка" / "файл.txt"
        assert extracted.exists(), "файл не распакован"
        assert "архив ок" in extracted.read_text(encoding="utf-8"), "неверное содержимое"

        evil = folder / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../evil.txt", "bad")
        try:
            execute_tool(
                "extract_archive",
                {"path": "selftest/evil.zip", "dest": "selftest/evil"},
                ctx,
            )
        except ToolError:
            pass
        else:
            raise AssertionError("zip-slip не заблокирован")
        return "распаковка и защита от zip-slip работают"

    check("Архивы и zip-slip", check_archive)

    def check_shell():
        ctx = Context(test_settings, lambda description: True, AuditLog(test_settings.data_path / "audit.log"))
        output = execute_tool("run_shell", {"command": "echo atlas-ok"}, ctx)
        assert "atlas-ok" in output, output
        return output.splitlines()[0]

    check("Команды оболочки", check_shell)

    def check_python():
        ctx = Context(test_settings, lambda description: True, AuditLog(test_settings.data_path / "audit.log"))
        output = execute_tool("run_python", {"code": "print(6 * 7)"}, ctx)
        assert "42" in output, output
        shell_output = execute_tool(
            "run_shell",
            {"command": 'python -c "import sys; print(sys.prefix)"'},
            ctx,
        )
        assert sys.prefix.lower() in shell_output.lower(), shell_output
        return "run_python возвращает 42, shell использует окружение агента"

    check("Python-окружение", check_python)

    def check_registry():
        specs = tool_specs()
        assert len(specs) >= 20, f"инструментов только {len(specs)}"
        names = {spec["function"]["name"] for spec in specs}
        required = {
            "read_file",
            "write_file",
            "run_shell",
            "run_python",
            "web_fetch",
            "memory_save",
            "subagent",
            "download_file",
            "extract_archive",
            "research_error",
            "lesson_save",
            "lesson_search",
            "plan_set",
            "plan_update",
            "worker_add",
            "worker_status",
            "worker_result",
            "worker_wait",
        }
        missing = required - names
        assert not missing, f"нет инструментов: {missing}"
        return f"{len(specs)} инструментов зарегистрировано"

    check("Реестр инструментов", check_registry)

    def check_plan_and_panel():
        ctx = Context(
            test_settings,
            lambda description: True,
            AuditLog(test_settings.data_path / "audit.log"),
        )
        execute_tool(
            "plan_set",
            {"tasks": ["собрать DNS", "проверить порты", "собрать отчёт"]},
            ctx,
        )
        assert len(ctx.plan) == 3, "план не сохранён"
        execute_tool("plan_update", {"index": 1, "status": "done"}, ctx)
        assert ctx.plan[0]["status"] == "done", "статус шага не обновлён"

        from .panel import PanelSession

        panel = PanelSession(test_settings, force=True)
        panel.agent = type("Demo", (), {"role_name": "Тест", "skill": ""})()
        panel.welcome()
        panel.begin_turn("проверь работу панели")
        panel.renderer.handle("start", {"depth": 0})
        panel.renderer.handle("plan", {"depth": 0, "tasks": ctx.plan})
        panel.renderer.handle("tool_call", {"depth": 0, "name": "read_file", "args": {"path": "a.txt"}})
        panel.renderer.handle(
            "tool_result",
            {"depth": 0, "name": "read_file", "ok": True, "seconds": 0.1, "output": "ok"},
        )
        panel.renderer.handle(
            "subagent_task_queued",
            {"depth": 1, "worker": 1, "task": "проверить порт 22", "brief": "nc"},
        )
        panel.renderer.handle(
            "subagent_task_start",
            {"depth": 1, "worker": 1, "task": "проверить порт 22", "brief": "nc"},
        )
        panel.renderer.handle(
            "subagent_task_done",
            {
                "depth": 1,
                "worker": 1,
                "task": "проверить порт 22",
                "ok": True,
                "seconds": 0.4,
                "output": "22 открыт",
            },
        )
        snapshot = panel.render_snapshot(122, 38)
        assert "ПЛАН" in snapshot, "в панели нет плана"
        assert "read_file" in snapshot, "в панели нет карточки инструмента"
        assert "проверь работу панели" in snapshot, "в панели нет задачи"
        assert "ПОМОЩНИКИ" in snapshot, "в панели нет блока помощников"
        assert "#1" in snapshot, "в панели нет карточки помощника"
        return "план, инструменты и живые карточки помощников отрисовываются"

    check("Панель агента и план", check_plan_and_panel)

    def check_background_workers():
        from .tools.subagent import add_background_workers

        agent = Agent(
            test_settings,
            confirm=lambda description: True,
            llm=ScriptedLLM([]),
        )
        records = add_background_workers(
            agent.ctx,
            ["посчитать 2+2", "проверить готовность отчёта"],
        )
        assert len(records) == 2, "помощники не созданы"
        assert all(record.id > 0 for record in records), "нет номеров помощников"
        status = execute_tool("worker_status", {}, agent.ctx)
        assert "всего 2" in status, status
        reports = execute_tool(
            "worker_wait",
            {"ids": [record.id for record in records], "timeout": 20},
            agent.ctx,
        )
        assert "2/2" in reports, reports
        assert reports.count("Готово.") >= 2, reports
        one = execute_tool("worker_result", {"id": records[0].id}, agent.ctx)
        assert "Готово." in one, one
        return "2 фоновых помощника отработали, отчёты собраны"

    check("Фоновые помощники", check_background_workers)

    def check_worker_tool_loop():
        calls = {"root": 0}

        class RoutingLLM:
            def chat(self, messages, tools=None, on_token=None):
                system = str(messages[0].get("content", "")) if messages else ""
                if "фоновый помощник ATLAS" in system:
                    return {
                        "role": "assistant",
                        "content": "Отчёт помощника: связь есть, всё ок.",
                    }
                calls["root"] += 1
                if calls["root"] == 1:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _call(
                                "w1",
                                "worker_add",
                                {
                                    "tasks": ["проверить связь"],
                                    "brief": "ответь коротко, без инструментов",
                                },
                            )
                        ],
                    }
                if calls["root"] == 2:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [_call("w2", "worker_wait", {"timeout": 20})],
                    }
                return {
                    "role": "assistant",
                    "content": "Итог: помощник сообщил, что всё ок.",
                }

        agent = Agent(
            test_settings,
            confirm=lambda description: True,
            llm=RoutingLLM(),
        )
        answer = agent.ask("Запусти фонового помощника и дождись отчёта.")
        assert "всё ок" in answer, answer
        pool = agent.ctx.worker_pool
        assert pool is not None and len(pool.records) == 1, pool
        record = next(iter(pool.records.values()))
        assert record.status == "done", record.status
        assert "всё ок" in record.result, record.result
        return "worker_add и worker_wait работают через цикл агента"

    check("Фоновые помощники через инструменты", check_worker_tool_loop)

    def check_plan_consistency():
        calls = {"n": 0}

        class PlanLLM:
            def chat(self, messages, tools=None, on_token=None):
                calls["n"] += 1
                n = calls["n"]
                if n == 1:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _call(
                                "p1",
                                "plan_set",
                                {"tasks": ["проверить", "установить", "подтвердить"]},
                            )
                        ],
                    }
                if n == 2:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _call(
                                "p2",
                                "plan_update",
                                {"index": 2, "status": "done"},
                            )
                        ],
                    }
                if n == 3:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _call(
                                "p3",
                                "plan_update",
                                {"index": 3, "status": "done"},
                            )
                        ],
                    }
                if n == 4:
                    return {
                        "role": "assistant",
                        "content": "Все шаги выполнены: 3/3.",
                    }
                if n == 5:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _call(
                                "p4",
                                "plan_update",
                                {"index": 1, "status": "done"},
                            )
                        ],
                    }
                return {"role": "assistant", "content": "Готово: 3/3."}

        agent = Agent(
            test_settings,
            confirm=lambda description: True,
            llm=PlanLLM(),
        )
        answer = agent.ask("Закрой все шаги плана.")
        assert calls["n"] == 6, calls
        assert all(item["status"] == "done" for item in agent.ctx.plan), agent.ctx.plan
        assert "3/3" in answer, answer

        from .panel import PanelSession

        panel = PanelSession(test_settings, force=True)
        panel.agent = type("Demo", (), {"role_name": "Тест", "skill": ""})()
        panel.begin_turn("проверка незакрытого плана")
        panel.plan = [
            {"text": "шаг 1", "status": "pending", "note": ""},
            {"text": "шаг 2", "status": "done", "note": ""},
            {"text": "шаг 3", "status": "done", "note": ""},
        ]
        panel.renderer.handle(
            "answer", {"depth": 0, "text": "Всё готово, 3/3."}
        )
        summary = [entry for entry in panel.entries if entry.kind == "summary"][-1]
        assert "завершено" in summary.text, summary.text
        assert "не закрыто 1" in summary.text, summary.text
        assert any(
            entry.kind == "notice" and "не отмечены" in entry.text
            for entry in panel.entries
        ), [entry.text for entry in panel.entries]
        assert "план 2/3" in panel._header(122), panel._header(122)
        return "незакрытый план не выдаётся за готовый, панель предупреждает"

    check("Согласованность плана", check_plan_consistency)

    def check_skills():
        skills = Skills(test_settings.skills_path).list()
        return f"навыков в каталоге: {len(skills)}"

    check("Каталог навыков", check_skills)

    shutil.rmtree(root, ignore_errors=True)

    failed = 0
    for name, ok, detail in checks:
        if ok:
            print(f"[ok]   {name} — {detail}")
        else:
            failed += 1
            print(f"[FAIL] {name} — {detail}")
    print("=" * 48)
    print(f"Итог: {len(checks) - failed}/{len(checks)} проверок пройдено.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    from .settings import load_settings

    raise SystemExit(run_selftest(load_settings()))