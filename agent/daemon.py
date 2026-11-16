from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .core import Agent
from .settings import ROOT, Settings
from .utils import preview, read_text, write_text


def run_daemon(
    settings: Settings,
    auto: bool = False,
    interval: float | None = None,
    once: bool = False,
) -> int:
    """Следит за папкой inbox/ и выполняет каждый файл как отдельную задачу."""
    inbox = ROOT / "inbox"
    outbox = ROOT / "outbox"
    done = inbox / "done"
    for path in (inbox, outbox, done):
        path.mkdir(parents=True, exist_ok=True)

    pause = max(2.0, float(interval if interval is not None else settings.daemon_interval))

    def confirm(description: str) -> bool:
        if auto:
            return True
        print(f"  [пропуск] нужно подтверждение: {description}")
        return False

    def on_event(kind: str, data: dict) -> None:
        if kind == "tool_call":
            print(f"  > {data['name']} {preview(str(data.get('args', {})), 120)}", flush=True)
        elif kind == "tool_result":
            mark = "ok" if data.get("ok") else "err"
            print(f"  < {mark}: {preview(str(data.get('output', '')), 120)}", flush=True)
        elif kind == "notice":
            print(f"  ! {data.get('text', '')}", flush=True)

    agent = Agent(settings, confirm=confirm, on_event=on_event)

    # Восстанавливаем задачи, прерванные в прошлый раз.
    for stale in inbox.glob("*.processing"):
        try:
            stale.rename(stale.with_suffix(""))
        except OSError:
            pass

    print(f"{settings.name}: режим автоматизации запущен")
    print(f"  Вход:  {inbox}")
    print(f"  Выход: {outbox}")
    print(f"  Интервал: {pause:.0f} с. Остановка: Ctrl+C")
    if once:
        print("  Режим: однократная обработка очереди (--once)")
    if settings.confirm == "always" and not auto:
        print("  ВНИМАНИЕ: confirm=always без --yes — опасные действия будут отклоняться.")

    try:
        while True:
            tasks = sorted(
                path
                for path in inbox.iterdir()
                if path.is_file()
                and path.suffix.lower() in (".txt", ".md")
                and not path.name.startswith((".", "_"))
                and not path.name.endswith(".processing")
            )
            for task_path in tasks:
                working = task_path.with_suffix(task_path.suffix + ".processing")
                try:
                    task_path.rename(working)
                except OSError:
                    continue

                task_text = read_text(working).strip()
                print(f"\n[{datetime.now():%H:%M:%S}] Задача: {task_path.name}")
                agent.reset()
                if not task_text:
                    answer = "Пустая задача."
                else:
                    try:
                        answer = agent.ask(task_text)
                    except Exception as exc:  # daemon не должен падать из-за одной задачи
                        answer = f"ОШИБКА: {type(exc).__name__}: {exc}"

                result_path = outbox / f"{task_path.stem}.md"
                write_text(
                    result_path,
                    f"# Задача\n\n{task_text or '(пусто)'}\n\n"
                    f"# Результат\n\n{answer}\n\n"
                    f"---\n_Выполнено: {datetime.now():%d.%m.%Y %H:%M}_\n",
                )
                _move_to_done(working, done)
                print(f"  Результат: {result_path}")

            if once:
                return 0
            time.sleep(pause)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0


def _move_to_done(source: Path, done: Path) -> None:
    target = done / source.name.replace(".processing", "")
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = done / f"{target.stem}-{stamp}{target.suffix}"
    try:
        source.rename(target)
    except OSError:
        pass