"""Общие исключения агента."""

from __future__ import annotations


class AgentError(RuntimeError):
    """Базовая ошибка агента."""


class ToolError(AgentError):
    """Ошибка инструмента (показывается модели, а не роняет агента)."""


class LLMError(AgentError):
    """Ошибка обращения к модели."""