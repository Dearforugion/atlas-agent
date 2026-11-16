from __future__ import annotations

from pathlib import Path

from .context import Context
from .errors import ToolError
from .safety import is_inside


def ensure_path(ctx: Context, raw: str | None = ".", must_exist: bool = False) -> Path:
    """Превращает путь в абсолютный и не пускает за пределы рабочей папки."""
    text = str(raw or ".").strip() or "."
    path = Path(text)
    if not path.is_absolute():
        path = ctx.workspace / path
    path = path.resolve()

    if not is_inside(ctx.workspace, path) and not ctx.settings.allow_outside_workspace:
        raise ToolError(
            f"Путь вне рабочей папки запрещён: {path}\n"
            f"Рабочая папка: {ctx.workspace}\n"
            "Если доступ реально нужен, включи allow_outside_workspace в config.json."
        )
    if must_exist and not path.exists():
        raise ToolError(f"Путь не найден: {path}")
    return path


def gate_if_outside(ctx: Context, path: Path, action: str) -> None:
    if not is_inside(ctx.workspace, path):
        ctx.gate(f"{action} вне рабочей папки: {path}", risky=True)