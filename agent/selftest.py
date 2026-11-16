from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from .audit import AuditLog
from .context import Context
from .core import Agent
from .errors import ToolError
from .knowledge import Memory, Skills
from .settings import Settings
from .tools import TOOLS, execute_tool, tool_specs


class ScriptedLLM:
    """Фальшивая модель для проверки цикла без интернета."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.index = 0
        self.requests: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self.requests.append(messages)
        if self.index >= len(self.script):
            return {"role": "assistant", "content": "Готово."}
        message = self.script[self.index]
        self.index += 1
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
        required = {"read_file", "write_file", "run_shell", "run_python", "web_fetch", "memory_save", "subagent"}
        missing = required - names
        assert not missing, f"нет инструментов: {missing}"
        return f"{len(specs)} инструментов зарегистрировано"

    check("Реестр инструментов", check_registry)

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