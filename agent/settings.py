from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMSettings:
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3:8b"
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 600
    native_tools: bool = True


@dataclass
class Settings:
    name: str = "ATLAS"
    workspace: str = "workspace"
    data_dir: str = "data"
    max_steps: int = 100
    confirm: str = "smart"  # smart | always | never
    allow_outside_workspace: bool = False
    allow_private_web: bool = False
    allow_private_targets: bool = False
    shell_timeout: int = 1800
    max_tool_output: int = 20000
    memory_context: int = 30
    daemon_interval: float = 10.0
    system_extra: str = ""
    llm: LLMSettings = field(default_factory=LLMSettings)

    @property
    def workspace_path(self) -> Path:
        path = Path(self.workspace)
        return path if path.is_absolute() else (ROOT / path).resolve()

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir)
        return path if path.is_absolute() else (ROOT / path).resolve()

    @property
    def skills_path(self) -> Path:
        return ROOT / "skills"

    def ensure_dirs(self) -> None:
        for path in (
            self.workspace_path,
            self.data_path,
            self.data_path / "sessions",
            self.skills_path,
            ROOT / "inbox" / "done",
            ROOT / "outbox",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict:
        return asdict(self)


def load_settings(path: Path | None = None) -> Settings:
    config_path = Path(path) if path else ROOT / "config.json"
    raw: dict = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Ошибка в {config_path}: {exc}") from exc

    llm_raw = raw.pop("llm", {}) or {}
    fields = {
        key: value
        for key, value in raw.items()
        if key in Settings.__dataclass_fields__ and key != "llm"
    }
    settings = Settings(**fields)
    settings.llm = LLMSettings(
        **{
            key: value
            for key, value in llm_raw.items()
            if key in LLMSettings.__dataclass_fields__
        }
    )

    if os.getenv("AGENT_MODEL"):
        settings.llm.model = os.environ["AGENT_MODEL"]
    if os.getenv("AGENT_BASE_URL"):
        settings.llm.base_url = os.environ["AGENT_BASE_URL"]
    if os.getenv("AGENT_API_KEY"):
        settings.llm.api_key = os.environ["AGENT_API_KEY"]
    if os.getenv("AGENT_CONFIRM"):
        settings.confirm = os.environ["AGENT_CONFIRM"]
    if os.getenv("AGENT_NATIVE_TOOLS"):
        settings.llm.native_tools = os.environ["AGENT_NATIVE_TOOLS"].strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if settings.confirm not in ("smart", "always", "never"):
        settings.confirm = "smart"
    settings.ensure_dirs()
    return settings