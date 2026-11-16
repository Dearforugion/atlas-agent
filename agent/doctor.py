from __future__ import annotations

import sys
from pathlib import Path

from .llm import LLM
from .settings import ROOT, Settings
from .tools import TOOLS

_SETUP_HINT = """
Как подключить модель (выбери один вариант):

1) Ollama — локально и бесплатно (рекомендуется):
   winget install Ollama.Ollama
   ollama pull qwen3:8b
   config.json -> "base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": "qwen3:8b"

2) LM Studio — графический интерфейс:
   включи Local Server (порт 1234)
   config.json -> "base_url": "http://127.0.0.1:1234/v1", "api_key": "lm-studio", модель из списка

3) Облачный API (OpenAI / OpenRouter / DeepSeek и др.):
   задай переменную окружения AGENT_API_KEY, в config.json укажи base_url и model.
"""


def run_doctor(settings: Settings) -> int:
    problems = 0
    print(f"{settings.name} — диагностика\n" + "=" * 48)

    print(f"Python: {sys.version.split()[0]}")
    print(f"Интерпретатор: {sys.executable}")
    venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"Виртуальное окружение: {'да' if venv else 'НЕТ (запусти setup.ps1)'}")
    if not venv:
        problems += 1

    print(f"Корень проекта: {ROOT}")
    print(f"Рабочая папка: {settings.workspace_path}")
    try:
        probe = settings.workspace_path / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("Запись в рабочую папку: да")
    except OSError as exc:
        print(f"Запись в рабочую папку: ОШИБКА ({exc})")
        problems += 1

    print(f"Инструментов: {len(TOOLS)}")
    print(f"Навыков: {len(list(settings.skills_path.glob('*.md')))}")

    memory_file = settings.data_path / "memory.jsonl"
    memory_count = 0
    if memory_file.exists():
        memory_count = sum(1 for line in memory_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    print(f"Записей в памяти: {memory_count}")

    print(f"\nМодель: {settings.llm.model}")
    print(f"API: {settings.llm.base_url}")
    try:
        models = LLM(settings.llm).list_models()
    except Exception as exc:
        print(f"Связь с моделью: НЕДОСТУПНА\n  {exc}")
        print(_SETUP_HINT)
        return problems

    print(f"Связь с моделью: ОК (доступно моделей: {len(models)})")
    if models:
        short_list = ", ".join(models[:10])
        print(f"  {short_list}{' ...' if len(models) > 10 else ''}")
    if not any(_same_model(settings.llm.model, model) for model in models):
        print(
            f"  ВНИМАНИЕ: модели «{settings.llm.model}» нет в списке. "
            "Укажи существующую в config.json или загрузи её."
        )
    else:
        print(f"  Модель «{settings.llm.model}» найдена.")

    print("\nИтог: " + ("есть проблемы, см. выше." if problems else "всё готово."))
    return problems


def _same_model(requested: str, available: str) -> bool:
    if requested == available:
        return True
    if ":" not in requested and f"{requested}:latest" == available:
        return True
    if ":" not in available and f"{available}:latest" == requested:
        return True
    return False


if __name__ == "__main__":
    from .settings import load_settings

    raise SystemExit(run_doctor(load_settings()))