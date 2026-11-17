from __future__ import annotations

from ..errors import ToolError
from ..knowledge import Lessons, Memory, Skills
from . import tool


@tool(
    "memory_save",
    "Запомнить важный факт о пользователе, проекте или правилах навсегда.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Что запомнить (одно чёткое утверждение)"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Теги для поиска, например ['предпочтения', 'проект']",
            },
        },
        "required": ["text"],
    },
)
def memory_save(args: dict, ctx) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("Пустой текст.")
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    record = Memory(ctx.memory_path).add(text, tags)
    ctx.audit.write("memory_save", record_id=record["id"], text=text)
    return f"Запомнено ({record['id']}): {text}"


@tool(
    "memory_search",
    "Найти факты в долговременной памяти.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Сколько записей вернуть (до 20)"},
        },
        "required": ["query"],
    },
)
def memory_search(args: dict, ctx) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Пустой запрос.")
    limit = max(1, min(20, int(args.get("limit") or 5)))
    records = Memory(ctx.memory_path).search(query, limit=limit)
    if not records:
        return "В памяти ничего не найдено."
    lines = [f"{record['id']}: {record['text']}" for record in records]
    return "Найдено в памяти:\n" + "\n".join(lines)


@tool(
    "memory_list",
    "Показать последние записи долговременной памяти.",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Сколько записей показать (до 50)"},
        },
    },
)
def memory_list(args: dict, ctx) -> str:
    limit = max(1, min(50, int(args.get("limit") or 20)))
    records = Memory(ctx.memory_path).recent(limit)
    if not records:
        return "Память пуста."
    lines = [f"{record['id']} [{record.get('ts', '')}] {record['text']}" for record in records]
    return "\n".join(lines)


@tool(
    "memory_forget",
    "Удалить запись из долговременной памяти по id.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
def memory_forget(args: dict, ctx) -> str:
    record_id = str(args.get("id") or "").strip()
    if not record_id:
        raise ToolError("Не указан id.")
    removed = Memory(ctx.memory_path).forget(record_id)
    if not removed:
        raise ToolError(f"Запись с id {record_id} не найдена.")
    ctx.audit.write("memory_forget", record_id=record_id)
    return f"Удалено из памяти: {record_id}"


@tool(
    "list_skills",
    "Показать список доступных навыков (методик).",
    {"type": "object", "properties": {}},
)
def list_skills(args: dict, ctx) -> str:
    skills = Skills(ctx.skills_path).list()
    if not skills:
        return "Навыков пока нет."
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)


@tool(
    "load_skill",
    "Загрузить полный текст навыка перед выполнением похожей задачи.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def load_skill(args: dict, ctx) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("Не указано имя навыка.")
    found = Skills(ctx.skills_path).get(name)
    if not found:
        available = ", ".join(skill.name for skill in Skills(ctx.skills_path).list()) or "(нет)"
        raise ToolError(f"Навык «{name}» не найден. Доступны: {available}")
    meta, text = found
    ctx.audit.write("load_skill", name=meta.name)
    return f"Навык «{meta.name}»:\n\n{text}"


@tool(
    "save_skill",
    "Создать или обновить навык — markdown-инструкцию, которой агент будет пользоваться позже.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Короткое имя, например 'excel-reports'"},
            "description": {"type": "string", "description": "Одна строка: когда применять навык"},
            "content": {"type": "string", "description": "Тело навыка: пошаговая инструкция в markdown"},
            "overwrite": {"type": "boolean", "description": "Заменить существующий навык"},
        },
        "required": ["name", "description", "content"],
    },
)
def save_skill(args: dict, ctx) -> str:
    name = str(args.get("name") or "").strip()
    description = str(args.get("description") or "").strip()
    content = str(args.get("content") or "").strip()
    if not name or not content:
        raise ToolError("Нужны name и content.")
    if not description:
        description = content.splitlines()[0].lstrip("# ").strip()[:160]
    try:
        meta, path = Skills(ctx.skills_path).save(
            name, description, content, overwrite=bool(args.get("overwrite"))
        )
    except FileExistsError as exc:
        raise ToolError(str(exc)) from exc
    ctx.audit.write("save_skill", name=meta.name, path=str(path))
    return f"Навык сохранён: {meta.name} ({path}). Он появится в каталоге со следующего запроса."


@tool(
    "delete_skill",
    "Удалить навык по имени.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def delete_skill(args: dict, ctx) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("Не указано имя навыка.")
    ctx.gate(f"Удалить навык «{name}»", risky=True)
    if not Skills(ctx.skills_path).delete(name):
        raise ToolError(f"Навык «{name}» не найден.")
    ctx.audit.write("delete_skill", name=name)
    return f"Навык удалён: {name}"


@tool(
    "lesson_save",
    "Сохранить урок: какая ошибка возникла и какое решение реально помогло. "
    "Используй после того, как нашёл и применил исправление проблемы, чтобы не "
    "повторять её в будущем.",
    {
        "type": "object",
        "properties": {
            "problem": {"type": "string", "description": "Симптом или текст ошибки"},
            "solution": {"type": "string", "description": "Что именно помогло: команды, настройки, обход"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Теги для поиска, например ['python', 'path', 'windows']",
            },
            "source": {"type": "string", "description": "Ссылка на документацию или issue, если была"},
        },
        "required": ["problem", "solution"],
    },
)
def lesson_save(args: dict, ctx) -> str:
    problem = str(args.get("problem") or "").strip()
    solution = str(args.get("solution") or "").strip()
    if not problem or not solution:
        raise ToolError("Нужны problem и solution.")
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    try:
        record, created = Lessons(ctx.lessons_path).add(
            problem, solution, tags, str(args.get("source") or "")
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ctx.audit.write("lesson_save", record_id=record["id"], created=created)
    action = "Сохранён новый урок" if created else "Обновлён урок"
    return f"{action} ({record['id']}): {problem[:160]}"


@tool(
    "lesson_search",
    "Найти в уроках самообучения похожую ошибку и готовое решение. "
    "Вызывай перед тем, как чинить проблему, с которой уже мог сталкиваться.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Текст ошибки, инструмент или симптом"},
            "limit": {"type": "integer", "description": "Сколько уроков вернуть (до 10)"},
        },
        "required": ["query"],
    },
)
def lesson_search(args: dict, ctx) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Пустой запрос.")
    limit = max(1, min(10, int(args.get("limit") or 3)))
    records = Lessons(ctx.lessons_path).search(query, limit=limit)
    if not records:
        return "Похожих уроков нет. Если найдёшь решение — сохрани его через lesson_save."
    parts = []
    for record in records:
        parts.append(
            f"[{record['id']}] {record.get('problem', '')}\n"
            f"  Решение: {record.get('solution', '')}"
            + (f"\n  Источник: {record['source']}" if record.get("source") else "")
        )
    return "Найденные уроки:\n\n" + "\n\n".join(parts)


@tool(
    "lesson_list",
    "Показать последние сохранённые уроки самообучения.",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Сколько уроков показать (до 50)"},
        },
    },
)
def lesson_list(args: dict, ctx) -> str:
    limit = max(1, min(50, int(args.get("limit") or 10)))
    records = Lessons(ctx.lessons_path).recent(limit)
    if not records:
        return "Уроков пока нет."
    return "\n".join(
        f"{record['id']} [{record.get('ts', '')}] {record.get('problem', '')} -> "
        f"{record.get('solution', '')}"
        for record in records
    )