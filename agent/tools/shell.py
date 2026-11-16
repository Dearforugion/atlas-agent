from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from ..errors import ToolError
from ..paths import ensure_path
from ..safety import dangerous_reason, offensive_tool_in
from ..scope import extract_candidate_targets, is_local_lab_target, load_scope
from ..utils import decode_output, write_text
from . import tool


@tool(
    "run_shell",
    "Выполнить команду оболочки (Windows cmd.exe) в рабочей папке.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Команда для cmd.exe"},
            "cwd": {"type": "string", "description": "Рабочая папка для команды (по умолчанию корень рабочей папки)"},
            "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию из конфига)"},
        },
        "required": ["command"],
    },
)
def run_shell(args: dict, ctx) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("Пустая команда.")

    reason = dangerous_reason(command)
    if reason:
        ctx.gate(f"ОПАСНАЯ команда ({reason}): {command}", risky=True)

    # Скоуп-гейт: если команда запускает наступательный инструмент, его цели
    # обязаны быть в авторизованном скоупе. Жёсткий отказ, не обходится авто-yes.
    offensive = offensive_tool_in(command)
    if offensive:
        targets = extract_candidate_targets(command)
        lab_allowed = (
            bool(ctx.settings.allow_private_targets)
            and bool(targets)
            and all(is_local_lab_target(target) for target in targets)
        )
        if lab_allowed:
            ctx.audit.write("scope_lab_allow", tool=offensive, command=command, targets=targets)
        else:
            scope = load_scope(ctx.settings.data_path)
            if scope.is_empty():
                raise ToolError(
                    f"Команда содержит наступательный инструмент «{offensive}», но скоуп пуст. "
                    "Запуск по целям без авторизации запрещён. Добавь цель через `agent scope ...` "
                    "и авторизуй скоуп, либо используй инструмент pentest_run."
                )
            if not targets:
                ctx.gate(
                    f"Наступательный инструмент «{offensive}» без явной цели в команде: {command}. "
                    "Убедись, что цель — в скоупе.",
                    risky=True,
                )
            else:
                ok, why = scope.check_many(targets)
                if not ok:
                    ctx.audit.write("scope_block", tool=offensive, command=command, reason=why)
                    raise ToolError(
                        f"ОТКАЗАНО скоупом: {why}. Команда с «{offensive}» не запущена — "
                        "цель вне авторизованного скоупа. Я не тестирую цели без разрешения."
                    )

    cwd = ensure_path(ctx, args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        raise ToolError(f"cwd не является папкой: {cwd}")
    timeout = _clamp_timeout(args.get("timeout"), ctx.settings.shell_timeout)

    if os.name == "nt":
        command_line = f"chcp 65001 >nul & {command}"
        popen_kwargs = {"shell": True}
    else:
        command_line = command
        popen_kwargs = {"shell": True, "executable": "/bin/sh"}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Команды python/pip должны работать в личном окружении агента.
    venv_scripts = Path(sys.executable).resolve().parent
    env["PATH"] = str(venv_scripts) + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_scripts.parent)

    ctx.audit.write("run_shell", command=command, cwd=str(cwd))
    try:
        proc = subprocess.run(
            command_line,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            env=env,
            **popen_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        partial = decode_output(exc.stdout) + decode_output(exc.stderr)
        raise ToolError(f"Команда прервана по таймауту ({timeout} с).\nЧастичный вывод:\n{partial[:3000]}") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"Не удалось запустить оболочку: {exc}") from exc

    stdout = decode_output(proc.stdout).strip()
    stderr = decode_output(proc.stderr).strip()
    parts = [f"exit code: {proc.returncode}"]
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(пустой вывод)")
    return "\n".join(parts)


@tool(
    "run_python",
    "Выполнить Python-код в личном окружении агента и вернуть вывод.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python-код"},
            "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 60)"},
        },
        "required": ["code"],
    },
)
def run_python(args: dict, ctx) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        raise ToolError("Пустой код.")
    timeout = _clamp_timeout(args.get("timeout"), 60, minimum=5, maximum=900)

    tmp_dir = ctx.workspace / ".agent_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_dir / f"snippet_{uuid.uuid4().hex[:10]}.py"
    write_text(script, code)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    ctx.audit.write("run_python", script=str(script), chars=len(code))
    try:
        proc = subprocess.run(
            [sys.executable or "python", str(script)],
            cwd=str(ctx.workspace),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = decode_output(exc.stdout) + decode_output(exc.stderr)
        raise ToolError(f"Python-код прерван по таймауту ({timeout} с).\nЧастичный вывод:\n{partial[:3000]}") from exc
    finally:
        script.unlink(missing_ok=True)

    stdout = decode_output(proc.stdout).strip()
    stderr = decode_output(proc.stderr).strip()
    parts = [f"exit code: {proc.returncode}"]
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(пустой вывод)")
    return "\n".join(parts)


def _clamp_timeout(value, default: int, minimum: int = 5, maximum: int = 1800) -> int:
    try:
        result = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        result = int(default)
    return max(minimum, min(maximum, result))