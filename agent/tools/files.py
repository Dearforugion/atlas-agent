from __future__ import annotations

import hashlib
import shutil
from itertools import islice
from pathlib import Path

from ..errors import ToolError
from ..paths import ensure_path, gate_if_outside
from ..safety import is_inside
from ..utils import read_text
from . import tool

MAX_READ_BYTES = 20_000_000


def _display(ctx, path: Path) -> str:
    try:
        return str(path.relative_to(ctx.workspace))
    except ValueError:
        return str(path)


def _size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} Б"
        value /= 1024
    return f"{num_bytes} Б"


@tool(
    "list_dir",
    "Показать содержимое папки в рабочем пространстве.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь относительно рабочей папки. По умолчанию '.'"},
            "recursive": {"type": "boolean", "description": "Показывать вложенные файлы"},
        },
    },
)
def list_dir(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path", "."), must_exist=True)
    if path.is_file():
        stat = path.stat()
        return f"[file] {_display(ctx, path)}  ({_size(stat.st_size)}, изменён {stat.st_mtime:.0f})"
    if not path.is_dir():
        raise ToolError(f"Это не папка: {_display(ctx, path)}")

    recursive = bool(args.get("recursive", False))
    entries: list[str] = []
    iterator = path.rglob("*") if recursive else path.iterdir()
    for entry in islice(iterator, 500):
        try:
            suffix = "/" if entry.is_dir() else ""
            if entry.is_file():
                detail = f"  ({_size(entry.stat().st_size)})"
            else:
                detail = ""
            entries.append(f"[{'dir' if entry.is_dir() else 'file'}] {_display(ctx, entry)}{suffix}{detail}")
        except OSError:
            continue
    if not entries:
        return f"{_display(ctx, path)}: пусто"
    header = f"{_display(ctx, path)} ({len(entries)} записей):"
    return header + "\n" + "\n".join(sorted(entries))


@tool(
    "read_file",
    "Прочитать текстовый файл с номерами строк.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "start_line": {"type": "integer", "description": "С какой строки начать (с 1)"},
            "max_lines": {"type": "integer", "description": "Сколько строк вернуть (по умолчанию 400)"},
        },
        "required": ["path"],
    },
)
def read_file(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"), must_exist=True)
    if not path.is_file():
        raise ToolError(f"Это не файл: {_display(ctx, path)}")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(f"Файл слишком большой ({_size(size)}). Используй поиск или прочитай часть через run_python.")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ToolError(f"Похоже, файл бинарный ({_size(size)}). Читать как текст нельзя.")

    content = read_text(path)
    lines = content.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    limit = max(1, min(2000, int(args.get("max_lines") or 400)))
    end = min(len(lines), start + limit - 1)
    body = "\n".join(f"{number:>5}: {lines[number - 1]}" for number in range(start, end + 1))
    if not body:
        return f"{_display(ctx, path)}: пусто"
    footer = "" if end >= len(lines) else f"\n... (всего {len(lines)} строк, показаны {start}-{end})"
    return f"{_display(ctx, path)} (строки {start}-{end} из {len(lines)}):\n{body}{footer}"


@tool(
    "write_file",
    "Создать или перезаписать текстовый файл (можно дописать в конец).",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "content": {"type": "string", "description": "Текст файла"},
            "append": {"type": "boolean", "description": "Дописать в конец вместо перезаписи"},
        },
        "required": ["path", "content"],
    },
)
def write_file(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"))
    gate_if_outside(ctx, path, "Запись файла")
    content = args.get("content")
    if content is None:
        raise ToolError("Не передан content.")
    text = content if isinstance(content, str) else str(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a" if args.get("append") else "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    ctx.audit.write("write_file", path=str(path), chars=len(text), append=bool(args.get("append")))
    action = "дописано" if args.get("append") else ("перезаписано" if existed else "создано")
    return f"{action}: {_display(ctx, path)} ({len(text)} символов)"


@tool(
    "make_dir",
    "Создать папку (вместе с родительскими).",
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
def make_dir(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"))
    gate_if_outside(ctx, path, "Создание папки")
    path.mkdir(parents=True, exist_ok=True)
    return f"Папка готова: {_display(ctx, path)}"


@tool(
    "move_path",
    "Переместить или переименовать файл/папку.",
    {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "overwrite": {"type": "boolean", "description": "Заменить существующий объект"},
        },
        "required": ["source", "destination"],
    },
)
def move_path(args: dict, ctx) -> str:
    source = ensure_path(ctx, args.get("source"), must_exist=True)
    destination = ensure_path(ctx, args.get("destination"))
    gate_if_outside(ctx, destination, "Перемещение")
    if destination.exists():
        if not args.get("overwrite"):
            raise ToolError(f"Цель уже существует: {_display(ctx, destination)}. Укажи overwrite=true для замены.")
        ctx.gate(f"Заменить существующий объект: {_display(ctx, destination)}", risky=True)
        _remove(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    ctx.audit.write("move_path", source=str(source), destination=str(destination))
    return f"Перемещено: {_display(ctx, source)} -> {_display(ctx, destination)}"


@tool(
    "copy_path",
    "Скопировать файл или папку.",
    {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["source", "destination"],
    },
)
def copy_path(args: dict, ctx) -> str:
    source = ensure_path(ctx, args.get("source"), must_exist=True)
    destination = ensure_path(ctx, args.get("destination"))
    gate_if_outside(ctx, destination, "Копирование")
    if destination.exists():
        if not args.get("overwrite"):
            raise ToolError(f"Цель уже существует: {_display(ctx, destination)}. Укажи overwrite=true.")
        ctx.gate(f"Заменить существующий объект: {_display(ctx, destination)}", risky=True)
        _remove(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    ctx.audit.write("copy_path", source=str(source), destination=str(destination))
    return f"Скопировано: {_display(ctx, source)} -> {_display(ctx, destination)}"


@tool(
    "delete_path",
    "Удалить файл или папку.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "description": "Удалить папку со всем содержимым"},
        },
        "required": ["path"],
    },
)
def delete_path(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"), must_exist=True)
    if is_inside(ctx.workspace, path) and path == ctx.workspace:
        raise ToolError("Нельзя удалить саму рабочую папку.")
    gate_if_outside(ctx, path, "Удаление")
    if path.is_dir():
        has_children = any(path.iterdir())
        if has_children and not args.get("recursive"):
            raise ToolError("Папка не пуста. Укажи recursive=true, если действительно нужно удалить всё.")
        if has_children:
            ctx.gate(f"Рекурсивно удалить папку: {_display(ctx, path)}", risky=True)
    _remove(path)
    ctx.audit.write("delete_path", path=str(path), recursive=bool(args.get("recursive")))
    return f"Удалено: {_display(ctx, path)}"


@tool(
    "search_text",
    "Найти текст во файлах рабочей папки (регистронезависимо).",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Что искать"},
            "path": {"type": "string", "description": "Где искать, по умолчанию вся рабочая папка"},
            "glob": {"type": "string", "description": "Маска файлов, например '**/*.py'"},
            "max_results": {"type": "integer", "description": "Сколько совпадений вернуть (до 100)"},
        },
        "required": ["query"],
    },
)
def search_text(args: dict, ctx) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Пустой запрос.")
    root = ensure_path(ctx, args.get("path", "."), must_exist=True)
    pattern = str(args.get("glob") or "**/*")
    limit = max(1, min(100, int(args.get("max_results") or 40)))
    needle = query.lower()
    results: list[str] = []
    scanned = 0

    for candidate in islice(root.glob(pattern), 2000):
        if len(results) >= limit:
            break
        if not candidate.is_file():
            continue
        scanned += 1
        try:
            if candidate.stat().st_size > 1_000_000:
                continue
            data = candidate.read_bytes()
            if b"\x00" in data[:2048]:
                continue
            text = read_text(candidate)
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line.lower():
                results.append(f"{_display(ctx, candidate)}:{number}: {line.strip()[:300]}")
                if len(results) >= limit:
                    break

    if not results:
        return f"Ничего не найдено (проверено файлов: {scanned})."
    return f"Найдено {len(results)} совпадений:\n" + "\n".join(results)


@tool(
    "file_info",
    "Показать информацию о файле или папке.",
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
def file_info(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"), must_exist=True)
    stat = path.stat()
    kind = "папка" if path.is_dir() else "файл"
    return (
        f"{_display(ctx, path)}\n"
        f"тип: {kind}\n"
        f"размер: {_size(stat.st_size)}\n"
        f"изменён: {stat.st_mtime:.0f} (unix)\n"
        f"создан: {stat.st_ctime:.0f} (unix)"
    )


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@tool(
    "hash_file",
    "Посчитать MD5, SHA-1 и SHA-256 файла, показать размер и время изменения. Полезно для проверки целостности.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
        },
        "required": ["path"],
    },
)
def hash_file(args: dict, ctx) -> str:
    path = ensure_path(ctx, args.get("path"), must_exist=True)
    if not path.is_file():
        raise ToolError(f"Это не файл: {_display(ctx, path)}")
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    stat = path.stat()
    return (
        f"{_display(ctx, path)}\n"
        f"размер: {_size(size)}\n"
        f"изменён: {stat.st_mtime:.0f} (unix)\n"
        f"MD5:    {md5.hexdigest()}\n"
        f"SHA-1:  {sha1.hexdigest()}\n"
        f"SHA-256: {sha256.hexdigest()}"
    )