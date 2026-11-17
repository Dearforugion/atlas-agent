"""Параллельный запуск воркеров-агентов ATLAS (subagents).

Инструмент `subagent` создаёт несколько копий самого агента для независимых
подзадач (разные хосты, поддомены, IP) и запускает их параллельно.
Каждый воркер — отдельный ИИ со своей историей, но с теми же скоупом,
инструментами, подтверждениями и журналом действий, что и родитель.

Глубина вложенности ограничена (MAX_SUBAGENT_DEPTH в agent/core.py):
root -> subagent разрешено, subagent -> subagent — нет.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..errors import ToolError
from . import tool

MAX_SUBTASKS = 8
MAX_WORKERS = 4
MAX_BACKGROUND_ADD = 6
MAX_BACKGROUND_ACTIVE = 4
MAX_BACKGROUND_RECORDS = 32
MAX_WAIT_SECONDS = 600
DEFAULT_WAIT_SECONDS = 180
MIN_WAIT_SECONDS = 60
_EMIT_LOCK = threading.Lock()
_WORKER_IDS = itertools.count(1)
_WORKER_ID_LOCK = threading.Lock()


def _next_worker_id() -> int:
    with _WORKER_ID_LOCK:
        return next(_WORKER_IDS)


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
    worker_ids = [_next_worker_id() for _ in tasks]

    def _emit(kind: str, **data) -> None:
        if not ctx.on_event:
            return
        with _EMIT_LOCK:
            try:
                ctx.on_event(kind, {"depth": ctx.depth, **data})
            except Exception:
                pass

    def _worker_event(worker: int):
        """Прокидывает номер воркера во все события его агента."""

        def handler(kind: str, data: dict) -> None:
            if not ctx.on_event:
                return
            payload = dict(data)
            payload.setdefault("worker", worker)
            with _EMIT_LOCK:
                try:
                    ctx.on_event(kind, payload)
                except Exception:
                    pass

        return handler

    started_all = time.monotonic()
    _emit("subagent_start", count=len(tasks), workers=workers, ids=worker_ids)

    def _run(worker: int, task: str) -> tuple[str, bool, str]:
        handler = _worker_event(worker)
        _emit("subagent_task_start", worker=worker, task=task)
        started = time.monotonic()
        agent = Agent(
            settings=ctx.settings,
            confirm=ctx.confirm,
            on_event=handler,
            depth=depth,
        )
        agent.ctx = Context(
            settings=ctx.settings,
            confirm=ctx.confirm,
            audit=ctx.audit,
            session_id=ctx.session_id,
            depth=depth,
            on_event=handler,
        )
        agent.history = []
        try:
            answer = agent.ask(task)
        except Exception as exc:
            answer = f"ОШИБКА: {type(exc).__name__}: {exc}"
            _emit(
                "subagent_task_done",
                worker=worker,
                task=task,
                ok=False,
                seconds=time.monotonic() - started,
                output=answer,
            )
            return task, False, answer
        _emit(
            "subagent_task_done",
            worker=worker,
            task=task,
            ok=True,
            seconds=time.monotonic() - started,
            output=answer,
        )
        return task, True, answer

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_run, worker_ids[index], task)
            for index, task in enumerate(tasks)
        ]
        results = [future.result() for future in futures]

    _emit(
        "subagent_end",
        ok=sum(1 for _, ok, _ in results if ok),
        failed=sum(1 for _, ok, _ in results if not ok),
        seconds=time.monotonic() - started_all,
    )

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


# --------------------------------------------------------------------------
# План задачи: живые чек-листы для панели агента.
# --------------------------------------------------------------------------

_PLAN_STATUSES = ("pending", "active", "done", "failed")
_PLAN_ALIASES = {
    "in_progress": "active",
    "in-progress": "active",
    "start": "active",
    "started": "active",
    "completed": "done",
    "complete": "done",
    "ok": "done",
    "success": "done",
    "error": "failed",
    "fail": "failed",
    "cancel": "pending",
}


def _plan(ctx) -> list[dict]:
    plan = getattr(ctx, "plan", None)
    if not isinstance(plan, list):
        plan = []
        ctx.plan = plan
    return plan


def _plan_event(ctx) -> None:
    """Отправляет панели актуальный план; воркеры свой план не навязывают."""
    if not ctx.on_event:
        return
    payload = {
        "tasks": [dict(item) for item in _plan(ctx)],
        "depth": ctx.depth,
    }
    with _EMIT_LOCK:
        try:
            ctx.on_event("plan", payload)
        except Exception:
            pass


@tool(
    "plan_set",
    "Составить план задачи для панели агента: 3–8 конкретных проверяемых шагов. "
    "Вызывай в начале сложной задачи (аудит, пентест, исследование, разработка), "
    "чтобы пользователь видел в панели, что уже сделано и сколько осталось. "
    "План заменяет предыдущий. Для простой задачи план не нужен.",
    {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Шаги по порядку, например ['собрать DNS', 'проверить порты', 'собрать отчёт'].",
            }
        },
        "required": ["tasks"],
    },
)
def plan_set(args: dict, ctx) -> str:
    raw = args.get("tasks")
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list) or not raw:
        raise ToolError("Нужен непустой список tasks: [].")
    items: list[dict] = []
    for entry in raw[:30]:
        text = entry.get("text") if isinstance(entry, dict) else entry
        text = str(text or "").strip()
        if not text:
            continue
        items.append({"text": text[:300], "status": "pending", "note": ""})
    if not items:
        raise ToolError("Все пункты плана пустые.")
    ctx.plan = items
    _plan_event(ctx)
    try:
        ctx.audit.write("plan_set", count=len(items))
    except Exception:
        pass
    return (
        f"План сохранён: {len(items)} шагов. "
        "Перед шагом вызывай plan_update(index=N, status='active'), "
        "после проверки результата — status='done', при сбое — 'failed'."
    )


@tool(
    "plan_update",
    "Отметить состояние шага плана: active (начал), done (проверено), failed (ошибка), pending. "
    "Вызывай сразу после выполнения шага, чтобы левая панель показывала реальный прогресс. "
    "index — номер шага, начиная с 1.",
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Номер шага, начиная с 1"},
            "status": {
                "type": "string",
                "enum": ["active", "done", "failed", "pending"],
                "description": "Новое состояние шага",
            },
            "note": {
                "type": "string",
                "description": "Короткий результат или причина ошибки (необязательно)",
            },
        },
        "required": ["index", "status"],
    },
)
def plan_update(args: dict, ctx) -> str:
    plan = _plan(ctx)
    if not plan:
        raise ToolError("План пуст. Сначала вызови plan_set со списком шагов.")
    try:
        index = int(args.get("index"))
    except (TypeError, ValueError):
        raise ToolError("index должен быть целым числом, начиная с 1.") from None
    if index < 1 or index > len(plan):
        raise ToolError(f"index вне плана: допустимо от 1 до {len(plan)}.")
    status = str(args.get("status") or "").strip().lower()
    status = _PLAN_ALIASES.get(status, status)
    if status not in _PLAN_STATUSES:
        raise ToolError("status должен быть active, done, failed или pending.")
    if status == "active":
        for position, item in enumerate(plan, 1):
            if position != index and item.get("status") == "active":
                item["status"] = "pending"
    item = plan[index - 1]
    item["status"] = status
    note = str(args.get("note") or "").strip()
    if note:
        item["note"] = note[:200]
    _plan_event(ctx)
    done = sum(1 for entry in plan if entry.get("status") == "done")
    failed = sum(1 for entry in plan if entry.get("status") == "failed")
    remaining = len(plan) - done
    return (
        f"Шаг {index} «{item.get('text', '')}» -> {status}. "
        f"Выполнено {done}/{len(plan)}, ошибок {failed}, осталось {remaining}."
    )


@tool(
    "plan_show",
    "Показать текущий план и статусы шагов. Используй, если потерял контекст плана.",
    {"type": "object", "properties": {}},
)
def plan_show(args: dict, ctx) -> str:
    plan = _plan(ctx)
    if not plan:
        return "План пуст. Для сложной задачи вызови plan_set."
    marks = {"done": "x", "active": ">", "failed": "!", "pending": " "}
    lines = ["Текущий план:"]
    for position, item in enumerate(plan, 1):
        note = f" — {item['note']}" if item.get("note") else ""
        lines.append(
            f"{position}. [{marks.get(str(item.get('status')), ' ')}] "
            f"{item.get('text', '')}{note}"
        )
    done = sum(1 for entry in plan if entry.get("status") == "done")
    lines.append(f"Прогресс: {done}/{len(plan)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Фоновые помощники: добавление на ходу, очередь, сбор результатов.
# --------------------------------------------------------------------------


@dataclass
class WorkerRecord:
    id: int
    task: str
    brief: str
    prompt: str
    status: str = "queued"
    created: float = field(default_factory=time.monotonic)
    started: float = 0.0
    finished: float = 0.0
    result: str = ""
    error: str = ""
    tools: int = 0
    tokens: int = 0
    last: str = ""
    phase: str = "в очереди"
    done: threading.Event = field(default_factory=threading.Event)


def _one_line(text: str, limit: int = 200) -> str:
    line = " ".join(str(text or "").split())
    if limit <= 0 or len(line) <= limit:
        return line
    return line[: limit - 1] + "…"


def _worker_prompt(worker_id: int, task: str, brief: str) -> str:
    lines = [
        f"Ты — фоновый помощник ATLAS #{worker_id}. Основной агент поручил тебе "
        "отдельную подзадачу и ждёт краткий отчёт.",
        f"Задача: {task}",
    ]
    if brief:
        lines.append(f"Пояснение от основного агента: {brief}")
    lines.append(
        "Работай самостоятельно: используй доступные инструменты, проверяй факты, "
        "не выходи за авторизованный скоуп и правила безопасности. Не задавай вопросов — "
        "действуй."
    )
    lines.append(
        "В конце верни отчёт: что сделал, что найдено, где результаты или файлы, "
        "какие были ошибки и что осталось."
    )
    return "\n".join(lines)


def _record_seconds(record: WorkerRecord) -> float:
    if not record.started:
        return 0.0
    end = record.finished or time.monotonic()
    return max(0.0, end - record.started)


def _record_status(record: WorkerRecord) -> str:
    return {
        "queued": "в очереди",
        "running": "работает",
        "done": "готово",
        "failed": "ошибка",
    }.get(record.status, record.status)


def _record_report(record: WorkerRecord, full: bool = False) -> str:
    timing = (
        "ждёт слот"
        if record.status == "queued"
        else f"{_record_seconds(record):.1f}с"
    )
    head = f"#{record.id} {_record_status(record)} · {timing} · инструментов {record.tools}"
    if record.status in ("queued", "running") and record.last:
        head += f" · сейчас: {record.last}"
    lines = [head, f"  задача: {_one_line(record.task, 240)}"]
    if full and record.brief:
        lines.append(f"  пояснение: {_one_line(record.brief, 400)}")
    if record.status == "done":
        result = record.result.strip()
        if full:
            lines.append("  результат:")
            lines.extend("    " + line for line in result.splitlines() or [""])
        else:
            lines.append(f"  результат: {_one_line(result, 280)}")
    elif record.status == "failed":
        error = (record.error or record.result).strip()
        if full:
            lines.append("  ошибка:")
            lines.extend("    " + line for line in error.splitlines() or [""])
        else:
            lines.append(f"  ошибка: {_one_line(error, 280)}")
    elif record.status == "queued":
        lines.append("  ждёт свободный слот (до 4 одновременно)")
    return "\n".join(lines)


class WorkerPool:
    """Очередь фоновых агентов: 4 работают одновременно, остальные ждут."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: dict[int, WorkerRecord] = {}
        self.queue: queue.Queue = queue.Queue()
        self.agent = None
        self.on_event = None
        self.threads: list[threading.Thread] = []

    def _ensure_threads(self) -> None:
        with self.lock:
            if self.threads:
                return
            for index in range(MAX_BACKGROUND_ACTIVE):
                thread = threading.Thread(
                    target=self._loop,
                    name=f"atlas-helper-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self.threads.append(thread)

    def _loop(self) -> None:
        while True:
            record = self.queue.get()
            if record is None:
                return
            try:
                self._run(record)
            finally:
                self.queue.task_done()

    def _emit(self, kind: str, **data) -> None:
        handler = self.on_event
        if not handler:
            return
        payload = {"depth": 1, **data}
        with _EMIT_LOCK:
            try:
                handler(kind, payload)
            except Exception:
                pass

    def _worker_event(self, record: WorkerRecord):
        """Прокидывает номер помощника в события его агента и ведёт прогресс."""

        def handler(kind: str, data: dict) -> None:
            payload = dict(data or {})
            payload["worker"] = record.id
            payload.setdefault("depth", 1)
            if kind == "tool_call":
                record.tools += 1
                record.last = str(payload.get("name") or "")
                record.phase = record.last
            elif kind == "tool_result":
                name = str(payload.get("name") or "")
                label = "ok" if payload.get("ok") else "ошибка"
                record.last = f"{name}: {label}"
                record.phase = "думает"
            elif kind == "step":
                record.phase = f"думает, шаг {payload.get('step')}"
            elif kind == "start":
                record.phase = "подключился"
            handler_root = self.on_event
            if not handler_root:
                return
            with _EMIT_LOCK:
                try:
                    handler_root(kind, payload)
                except Exception:
                    pass

        return handler

    def _run(self, record: WorkerRecord) -> None:
        root = self.agent
        if root is None:
            record.status = "failed"
            record.error = "нет корневого агента"
            record.done.set()
            return
        with self.lock:
            record.status = "running"
            record.started = time.monotonic()
            record.phase = "подключился"
        self._emit(
            "subagent_task_start",
            worker=record.id,
            task=record.task,
            brief=record.brief,
        )
        started = time.monotonic()
        ok = False
        output = ""
        try:
            worker = root.spawn(record.prompt)
            handler = self._worker_event(record)
            worker.on_event = handler
            worker.ctx.on_event = handler
            output = worker.ask(record.prompt)
            ok = True
        except Exception as exc:
            output = f"{type(exc).__name__}: {exc}"
        finally:
            seconds = max(0.0, time.monotonic() - started)
            with self.lock:
                record.status = "done" if ok else "failed"
                record.finished = time.monotonic()
                record.result = output if ok else ""
                record.error = "" if ok else output
                record.phase = "готово" if ok else "ошибка"
            self._emit(
                "subagent_task_done",
                worker=record.id,
                task=record.task,
                ok=ok,
                seconds=seconds,
                output=output,
            )
            record.done.set()

    def add(self, agent, task: str, brief: str = "") -> WorkerRecord:
        task = str(task or "").strip()
        if not task:
            raise ToolError("Пустая задача помощника.")
        brief = str(brief or "").strip()
        with self.lock:
            if len(self.records) >= MAX_BACKGROUND_RECORDS:
                removable = sorted(
                    record.id
                    for record in self.records.values()
                    if record.status in ("done", "failed")
                )
                while removable and len(self.records) >= MAX_BACKGROUND_RECORDS:
                    self.records.pop(removable.pop(0), None)
                if len(self.records) >= MAX_BACKGROUND_RECORDS:
                    raise ToolError(
                        "Слишком много фоновых помощников. Дождись завершения "
                        "части из них и повтори worker_add."
                    )
            worker_id = _next_worker_id()
            record = WorkerRecord(
                id=worker_id,
                task=task,
                brief=brief,
                prompt=_worker_prompt(worker_id, task, brief),
            )
            self.records[worker_id] = record
            self.agent = agent
            self.on_event = getattr(agent, "on_event", None)
        self._ensure_threads()
        self._emit(
            "subagent_task_queued",
            worker=worker_id,
            task=task,
            brief=brief,
        )
        self.queue.put(record)
        return record


def _pool(ctx) -> WorkerPool:
    pool = getattr(ctx, "worker_pool", None)
    if pool is None:
        if getattr(ctx, "agent", None) is None:
            raise ToolError("Нет доступа к агенту: фоновые помощники недоступны.")
        pool = WorkerPool()
        ctx.worker_pool = pool
    return pool


def _ids_arg(args: dict) -> list[int] | None:
    raw = args.get("ids")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        raw = [part for part in raw.replace(";", ",").split(",") if part.strip()]
    elif isinstance(raw, (int, float)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ToolError("ids должен быть списком номеров помощников.")
    ids: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            raise ToolError(f"Некорректный номер помощника: {item!r}.") from None
        if value not in ids:
            ids.append(value)
    return ids or None


def add_background_workers(ctx, tasks, brief: str = "") -> list[WorkerRecord]:
    """Создаёт фоновых помощников из CLI или инструмента."""
    if getattr(ctx, "depth", 0) > 0:
        raise ToolError("Фоновые помощники доступны только основному агенту.")
    agent = getattr(ctx, "agent", None)
    if agent is None:
        raise ToolError("Фоновые помощники доступны только основному агенту.")
    if isinstance(tasks, str):
        tasks = [tasks]
    clean: list[str] = []
    for item in list(tasks)[:MAX_BACKGROUND_ADD]:
        task = str(item or "").strip()
        if task and task not in clean:
            clean.append(task)
    if not clean:
        raise ToolError("Нужна хотя бы одна непустая задача помощнику.")
    pool = _pool(ctx)
    records = [pool.add(agent, task, brief) for task in clean]
    try:
        ctx.audit.write(
            "worker_add",
            count=len(records),
            ids=[record.id for record in records],
            tasks=[record.task for record in records],
        )
    except Exception:
        pass
    return records


def add_background_worker(ctx, task: str, brief: str = "") -> WorkerRecord:
    return add_background_workers(ctx, [task], brief)[0]


@tool(
    "worker_add",
    "Добавить фоновых помощников-агентов НА ХОДУ и сразу продолжить работу. "
    "Каждый помощник получает задачу и подробное пояснение brief, работает параллельно "
    "в своей истории с теми же инструментами и скоупом. До 4 работают одновременно, "
    "остальные ждут в очереди. После добавления продолжай свои шаги, а перед финальным "
    "ответом вызови worker_wait(ids=[...]) и учти отчёты. Для блокирующего параллельного "
    "запуска с немедленным результатом используй subagent.",
    {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Задачи помощникам. Можно строку с переносами или список.",
            },
            "brief": {
                "type": "string",
                "description": "Подробное пояснение от основного агента: что проверить, какими инструментами, что вернуть, границы скоупа.",
            },
            "explain": {
                "type": "string",
                "description": "То же, что brief (альтернативное имя).",
            },
        },
        "required": ["tasks"],
    },
)
def worker_add(args: dict, ctx) -> str:
    if getattr(ctx, "depth", 0) > 0:
        raise ToolError("Фоновые помощники доступны только основному агенту.")
    raw = args.get("tasks")
    if raw is None:
        raw = args.get("task")
    if isinstance(raw, str):
        raw = [line for line in raw.replace(";", "\n").splitlines() if line.strip()]
    if not isinstance(raw, list) or not raw:
        raise ToolError("Нужен непустой список tasks: [].")
    shared_brief = str(args.get("brief") or args.get("explain") or "").strip()
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw[:MAX_BACKGROUND_ADD]:
        if isinstance(item, dict):
            task = str(item.get("task") or item.get("text") or "").strip()
            brief = str(item.get("brief") or item.get("explain") or "").strip()
        else:
            task = str(item or "").strip()
            brief = ""
        if not task or task in seen:
            continue
        seen.add(task)
        pairs.append((task, brief or shared_brief))
    if not pairs:
        raise ToolError("Все задачи помощников пустые.")
    pool = _pool(ctx)
    agent = getattr(ctx, "agent", None)
    if agent is None:
        raise ToolError("Фоновые помощники доступны только основному агенту.")
    records = [pool.add(agent, task, brief) for task, brief in pairs]
    try:
        ctx.audit.write(
            "worker_add",
            count=len(records),
            ids=[record.id for record in records],
            tasks=[record.task for record in records],
        )
    except Exception:
        pass
    ids = ", ".join(f"#{record.id}" for record in records)
    lines = [
        f"Фоновые помощники добавлены: {len(records)} ({ids}). "
        "До 4 работают одновременно, остальные ждут в очереди.",
    ]
    for record in records:
        lines.append(f"  #{record.id}: {_one_line(record.task, 180)}")
    lines.append(
        "Продолжай свою работу. Перед финальным ответом вызови "
        f"worker_wait(ids=[{', '.join(str(record.id) for record in records)}], timeout=180) "
        "и учти отчёты помощников. Если помощник ещё работает, вызови worker_wait ещё раз; "
        "не завершай задачу с пустым результатом."
    )
    return "\n".join(lines)


@tool(
    "worker_status",
    "Показать состояние фоновых помощников: кто работает, кто в очереди, кто готов, "
    "что делает каждый и краткий результат. Используй между шагами, не блокирует.",
    {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Только эти номера помощников (по умолчанию все).",
            }
        },
    },
)
def worker_status(args: dict, ctx) -> str:
    pool = getattr(ctx, "worker_pool", None)
    if pool is None or not pool.records:
        return "Фоновые помощники не запускались. Добавь через worker_add."
    ids = _ids_arg(args)
    wanted = ids or sorted(pool.records)
    records = [pool.records[wid] for wid in wanted if wid in pool.records]
    missing = [wid for wid in wanted if wid not in pool.records]
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    lines = [
        f"Помощники: всего {len(records)} · работают {counts['running']} · "
        f"в очереди {counts['queued']} · готово {counts['done']} · "
        f"ошибок {counts['failed']}."
    ]
    if missing:
        lines.append(f"Не найдены номера: {', '.join('#' + str(wid) for wid in missing)}.")
    for record in records:
        lines.append(_record_report(record, full=False))
    return "\n".join(lines)


@tool(
    "worker_result",
    "Получить отчёт одного фонового помощника. Если он ещё работает, можно подождать "
    "до wait секунд (0 — не ждать).",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Номер помощника"},
            "wait": {
                "type": "integer",
                "description": "Сколько секунд ждать результата (0–600, по умолчанию 0).",
            },
        },
        "required": ["id"],
    },
)
def worker_result(args: dict, ctx) -> str:
    pool = getattr(ctx, "worker_pool", None)
    if pool is None or not pool.records:
        return "Фоновые помощники не запускались. Добавь через worker_add."
    try:
        wid = int(args.get("id"))
    except (TypeError, ValueError):
        raise ToolError("id должен быть целым номером помощника.") from None
    record = pool.records.get(wid)
    if record is None:
        known = ", ".join(f"#{item}" for item in sorted(pool.records))
        raise ToolError(f"Помощник #{wid} не найден. Известные: {known}.")
    try:
        wait = max(0.0, min(float(args.get("wait") or 0), MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        raise ToolError("wait должен быть числом секунд.") from None
    if wait and not record.done.is_set():
        record.done.wait(wait)
    if record.status in ("queued", "running"):
        return (
            _record_report(record, full=True)
            + f"\n\nПомощник ещё не готов. Подожди: worker_result(id={wid}, wait=60) "
            "или worker_wait(ids=[...])."
        )
    return _record_report(record, full=True)


@tool(
    "worker_wait",
    "Дождаться фоновых помощников и собрать их отчёты. Блокирует до timeout секунд "
    "(по умолчанию 180, при ожидании не меньше 60). Вызывай перед финальным ответом. "
    "Если помощник ещё работает, вызови worker_wait повторно с timeout=180; не завершай "
    "задачу с пустым результатом, пока не получишь отчёты.",
    {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Каких помощников ждать (по умолчанию всех активных).",
            },
            "timeout": {
                "type": "integer",
                "description": "Максимум секунд ожидания (по умолчанию 180, до 600; меньше 60 при ожидании округляется до 60).",
            },
        },
    },
)
def worker_wait(args: dict, ctx) -> str:
    pool = getattr(ctx, "worker_pool", None)
    if pool is None or not pool.records:
        return "Фоновые помощники не запускались. Добавь через worker_add."
    ids = _ids_arg(args)
    if ids is None:
        ids = [
            record.id
            for record in sorted(pool.records.values(), key=lambda item: item.id)
            if record.status in ("queued", "running")
        ]
    if not ids:
        finished = [
            record
            for record in sorted(pool.records.values(), key=lambda item: item.id)
            if record.status in ("done", "failed")
        ][-6:]
        if not finished:
            return "Нет активных помощников: все уже завершились. Используй worker_status."
        lines = [
            "Нет активных помощников: все уже завершились. Последние отчёты:"
        ]
        for record in finished:
            lines.append(_record_report(record, full=True))
        return "\n".join(lines)
    try:
        requested = float(
            args.get("timeout")
            if args.get("timeout") is not None
            else DEFAULT_WAIT_SECONDS
        )
    except (TypeError, ValueError):
        raise ToolError("timeout должен быть числом секунд.") from None
    if requested <= 0:
        timeout = 0.0
    else:
        timeout = min(max(requested, MIN_WAIT_SECONDS), MAX_WAIT_SECONDS)
    known = [wid for wid in ids if wid in pool.records]
    missing = [wid for wid in ids if wid not in pool.records]
    if not known:
        raise ToolError("Ни один из указанных помощников не найден.")
    start = time.monotonic()
    deadline = start + timeout
    for wid in known:
        record = pool.records[wid]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        record.done.wait(remaining)
    elapsed = time.monotonic() - start
    still = [
        wid
        for wid in known
        if pool.records[wid].status in ("queued", "running")
    ]
    done_count = len(known) - len(still)
    header = (
        f"Дождался {done_count}/{len(known)} помощников за {elapsed:.1f}с."
        if timeout
        else f"Статус помощников (без ожидания)."
    )
    lines = [header]
    if still:
        lines.append(
            "Ещё работают: "
            + ", ".join(f"#{wid} ({_record_status(pool.records[wid])})" for wid in still)
            + ". НЕ завершай ответ с пустым результатом: вызови "
            "worker_wait(ids=["
            + ", ".join(str(wid) for wid in still)
            + "], timeout=180) ещё раз и дождись отчёта."
        )
    if missing:
        lines.append(f"Не найдены номера: {', '.join('#' + str(wid) for wid in missing)}.")
    for wid in known:
        lines.append(_record_report(pool.records[wid], full=True))
    return "\n".join(lines)
