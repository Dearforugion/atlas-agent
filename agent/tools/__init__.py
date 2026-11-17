from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..errors import ToolError


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict[str, Any], Any], str]


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict):
    """Декоратор регистрации инструмента."""

    def decorator(fn: Callable[[dict[str, Any], Any], str]) -> Callable:
        if name in TOOLS:
            raise RuntimeError(f"Инструмент уже зарегистрирован: {name}")
        TOOLS[name] = Tool(name=name, description=description, parameters=parameters, fn=fn)
        return fn

    return decorator


def tool_specs() -> list[dict]:
    """Описание инструментов в формате OpenAI tools."""
    specs = []
    for entry in sorted(TOOLS.values(), key=lambda item: item.name):
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": entry.name,
                    "description": entry.description,
                    "parameters": entry.parameters,
                },
            }
        )
    return specs


def execute_tool(name: str, args: dict, ctx: Any) -> str:
    entry = TOOLS.get(name)
    if not entry:
        available = ", ".join(sorted(TOOLS))
        raise ToolError(f"Неизвестный инструмент «{name}». Доступны: {available}")
    if not isinstance(args, dict):
        raise ToolError("Аргументы инструмента должны быть JSON-объектом.")
    return entry.fn(args, ctx)


# Регистрация инструментов при импорте пакета.
from . import audit, files, knowledge_tools, net, pentest, pentest_extended, shell, subagent, web  # noqa: E402,F401