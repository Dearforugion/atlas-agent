from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog
from .errors import ToolError
from .settings import Settings


@dataclass
class Context:
    """Всё, что нужно инструментам: настройки, подтверждения, журнал."""

    settings: Settings
    confirm: Callable[[str], bool]
    audit: AuditLog
    session_id: str = ""
    depth: int = 0
    on_event: Callable[[str, dict], None] | None = None
    plan: list[dict] = field(default_factory=list)
    agent: Any = None
    worker_pool: Any = None

    @property
    def workspace(self) -> Path:
        return self.settings.workspace_path

    @property
    def memory_path(self) -> Path:
        return self.settings.data_path / "memory.jsonl"

    @property
    def skills_path(self) -> Path:
        return self.settings.skills_path

    @property
    def lessons_path(self) -> Path:
        return self.settings.data_path / "lessons.jsonl"

    def gate(self, description: str, risky: bool = False) -> None:
        """Спрашивает подтверждение, если действие рискованное."""
        mode = self.settings.confirm
        if mode == "never":
            allowed = True
        elif mode == "always":
            allowed = self.confirm(description)
        else:
            allowed = self.confirm(description) if risky else True

        self.audit.write(
            "confirmation",
            description=description,
            risky=risky,
            allowed=allowed,
        )
        if not allowed:
            raise ToolError(
                "Действие отклонено пользователем. Предложи безопасную альтернативу "
                "или объясни, зачем это нужно."
            )