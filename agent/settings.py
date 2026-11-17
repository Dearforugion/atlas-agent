from __future__ import annotations

import glob
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _augment_path() -> None:
    """Добавляет локальные каталоги утилит в PATH процесса.

    Python-инструменты ставятся в .venv\\Scripts, Go-утилиты — в
    %USERPROFILE%\\go\\bin, обёртки ATLAS — в tools\\. Без этого
    shutil.which() и дочерние процессы не видят установленные утилиты.
    """
    # Порядок важен: tools/ и go/bin идут раньше .venv\Scripts, иначе
    # Python-пакет httpx перекрывает ProjectDiscovery httpx.
    candidates = [
        ROOT / "tools",
        Path.home() / "go" / "bin",
        ROOT / ".venv" / "Scripts",
        ROOT / "tools" / "ruby" / "bin",
        Path("C:/Program Files (x86)/Nmap"),
        Path("C:/Program Files/Nmap"),
        Path("C:/Program Files/Git/bin"),
    ]
    # Ruby может быть установлен в нестандартное место.
    candidates.extend(Path(p) for p in glob.glob(r"C:\Ruby*\bin"))
    candidates.extend(Path(p) for p in glob.glob(str(ROOT / "tools" / "ruby" / "rubyinstaller-*" / "bin")))

    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    preferred: list[str] = []
    preferred_keys: set[str] = set()
    for directory in candidates:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        key = str(directory).rstrip("\\/").lower()
        if key not in preferred_keys:
            preferred.append(str(directory))
            preferred_keys.add(key)

    # Убираем эти каталоги из старого PATH и ставим их в начало.
    rest = [
        p
        for p in current
        if p.rstrip("\\/").lower() not in preferred_keys
    ]
    if preferred:
        os.environ["PATH"] = os.pathsep.join(preferred + rest)


@dataclass
class LLMSettings:
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3:8b"
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 600
    native_tools: bool = True
    stream: bool = True


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
    _augment_path()
    config_path = Path(path) if path else ROOT / "config.json"
    raw: dict = {}
    if config_path.exists():
        try:
            # utf-8-sig понимает и обычный UTF-8, и файл с BOM (его пишет
            # PowerShell 5.1 при ConvertTo-Json | Set-Content).
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Ошибка в {config_path}: {exc}") from exc
        except OSError as exc:
            raise SystemExit(f"Не удалось прочитать {config_path}: {exc}") from exc

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
    if os.getenv("ATLAS_NO_STREAM"):
        settings.llm.stream = False

    if settings.confirm not in ("smart", "always", "never"):
        settings.confirm = "smart"
    settings.ensure_dirs()
    return settings