"""Параллельный запуск воркеров-агентов ATLAS (subagents).

Инструмент `subagent` создаёт несколько копий самого агента для независимых
подзадач (разные хосты, поддомены, IP) и запускает их параллельно.
Каждый воркер — отдельный ИИ со своей историей, но с теми же скоупом,
инструментами, подтверждениями и журналом действий, что и родитель.

Глубина вложенности ограничена (MAX_SUBAGENT_DEPTH в agent/core.py):
root -> subagent разрешено, subagent -> subagent — нет.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..errors import ToolError
from . import tool

MAX_SUBTASKS = 8
MAX_WORKERS = 4


@tool(
    "subagent",
    "Запустить воркеров-агентов ATLAS для параллельного выполнения подзадач. "
    "Каждый воркер — отдельный ИИ со своей историей, но с теми же скоупом, инструментами и правилами, "
    "что и родительский агент. Используй для быстрого сканирования нескольких хостов/поддоменов: "
    "независимые цели проверяются параллельными воркерами, что ускоряет полный цикл аудита. "
    "Пример: tasks=['просканируй 192.168.56.10 и собери открытые порты', 'просканируй 192.168.56.11'].",
    {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список подзадач для параллельного выполнения (обычно одна подзадача — одна цель).",
            },
            "max_workers": {
                "type": "integer",
                "description": "Сколько воркеров запускать одновременно (по умолчанию все, максимум 4).",
            },
        },
        "required": ["tasks"],
    },
)
def subagent(args: dict, ctx) -> str:
    tasks_raw = args.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ToolError("Нужен непустой список задач tasks: [].")

    tasks: list[str] = []
    for item in tasks_raw[:MAX_SUBTASKS]:
        task = str(item or "").strip()
        if task and task not in tasks:
            tasks.append(task)
    if not tasks:
        raise ToolError("Список задач пуст.")

    # Импортируем здесь, чтобы не создавать циклическую зависимость на этапе загрузки.
    from ..core import MAX_SUBAGENT_DEPTH, Agent
    from ..context import Context

    depth = ctx.depth + 1
    if depth > MAX_SUBAGENT_DEPTH:
        raise ToolError(
            f"Достигнут лимит вложенности подзадач ATLAS (максимум {MAX_SUBAGENT_DEPTH} уровень). "
            f"Текущая глубина агента: {ctx.depth}. "
            "Воркер не запускается — слишком длинная цепочка агентов."
        )

    workers = max(1, min(len(tasks), int(args.get("max_workers") or len(tasks)), MAX_WORKERS))

    def _run(task: str) -> tuple[str, bool, str]:
        agent = Agent(
            settings=ctx.settings,
            confirm=ctx.confirm,
            on_event=ctx.on_event,
            depth=depth,
        )
        agent.ctx = Context(
            settings=ctx.settings,
            confirm=ctx.confirm,
            audit=ctx.audit,
            session_id=ctx.session_id,
            depth=depth,
            on_event=ctx.on_event,
        )
        agent.history = []
        try:
            answer = agent.ask(task)
        except Exception as exc:
            return task, False, f"ОШИБКА: {type(exc).__name__}: {exc}"
        return task, True, answer

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_run, tasks))

    parts: list[str] = []
    for task, ok, answer in results:
        parts.append(f"### Задача: {task}\nСтатус: {'выполнена' if ok else 'ошибка'}\n\n{answer}")
    combined = "\n\n---\n\n".join(parts)

    try:
        ctx.audit.write("subagent", tasks=tasks, depth=depth, ok=all(ok for _, ok, _ in results))
    except Exception:
        pass

    return (
        f"Запущено воркеров ATLAS параллельно: {len(tasks)} задач "
        f"({workers} одновременно, глубина вложенности: {depth}).\n\n{combined}"
    )
