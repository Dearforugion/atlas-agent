from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from itertools import islice
from pathlib import Path

from ..errors import ToolError
from ..paths import ensure_path, gate_if_outside
from ..safety import is_inside
from ..utils import read_text
from . import tool

MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024

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


def _strip_archive_suffix(name: str) -> str:
    lower = name.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def _archive_target(root: Path, member: str) -> Path:
    name = str(member or "").replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", name) or name.startswith("//"):
        raise ToolError(f"Абсолютный путь в архиве запрещён: {member}")
    target = (root / name).resolve()
    if not is_inside(root, target):
        raise ToolError(f"Архив пытается выйти за пределы папки распаковки: {member}")
    return target


def _zip_extract(archive: Path, dest: Path) -> tuple[int, int, int]:
    files = 0
    dirs = 0
    skipped = 0
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ToolError(
                f"В архиве {len(infos)} записей — больше лимита {MAX_ARCHIVE_ENTRIES}."
            )
        for info in infos:
            target = _archive_target(dest, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                dirs += 1
                continue
            total += int(info.file_size)
            if total > MAX_ARCHIVE_BYTES:
                raise ToolError("Распакованный архив превысил лимит 4 ГБ.")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            except (RuntimeError, NotImplementedError) as exc:
                raise ToolError(
                    f"Не удалось распаковать {info.filename}: {exc}. "
                    "Возможно, архив зашифрован или использует неподдерживаемое сжатие."
                ) from exc
            files += 1
    return files, dirs, skipped


def _tar_extract(archive: Path, dest: Path) -> tuple[int, int, int]:
    files = 0
    dirs = 0
    skipped = 0
    total = 0
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ToolError(
                f"В архиве {len(members)} записей — больше лимита {MAX_ARCHIVE_ENTRIES}."
            )
        for member in members:
            target = _archive_target(dest, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                dirs += 1
                continue
            if not member.isfile():
                # Симлинки и устройства пропускаем: они позволяют выйти
                # за пределы папки распаковки.
                skipped += 1
                continue
            total += int(member.size)
            if total > MAX_ARCHIVE_BYTES:
                raise ToolError("Распакованный архив превысил лимит 4 ГБ.")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                skipped += 1
                continue
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            files += 1
    return files, dirs, skipped


def _seven_zip() -> str | None:
    found = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "7-Zip" / "7z.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


@tool(
    "extract_archive",
    "Распаковать архив в рабочую папку: zip, tar, tar.gz/tgz, gz, а также 7z/rar "
    "при установленном 7-Zip. Защищено от zip-slip и архивных бомб.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к архиву"},
            "dest": {
                "type": "string",
                "description": "Папка распаковки. По умолчанию — рядом с архивом, по имени архива",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Разрешить распаковку в непустую папку",
            },
        },
        "required": ["path"],
    },
)
def extract_archive(args: dict, ctx) -> str:
    archive = ensure_path(ctx, args.get("path"), must_exist=True)
    if not archive.is_file():
        raise ToolError(f"Это не файл: {_display(ctx, archive)}")
    name = archive.name.lower()

    if name.endswith((".zip",)):
        kind = "zip"
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        kind = "tar"
    elif name.endswith(".gz"):
        kind = "gz"
    elif name.endswith((".7z", ".rar")):
        kind = "7z"
    else:
        raise ToolError(
            "Неизвестный формат архива. Поддерживаются: zip, tar, tar.gz/tgz, "
            "tar.bz2, tar.xz, gz, 7z и rar (для 7z/rar нужен 7-Zip)."
        )

    raw_dest = str(args.get("dest") or "").strip()
    if raw_dest:
        dest = ensure_path(ctx, raw_dest)
    else:
        dest = archive.parent / _strip_archive_suffix(archive.name)
    gate_if_outside(ctx, dest, "Распаковка архива")

    if dest.exists() and not dest.is_dir():
        raise ToolError(f"Цель распаковки — файл, а не папка: {_display(ctx, dest)}")
    if dest.is_dir() and any(dest.iterdir()) and not bool(args.get("overwrite")):
        raise ToolError(
            f"Папка распаковки не пуста: {_display(ctx, dest)}. "
            "Укажи overwrite=true или другую папку dest."
        )
    dest.mkdir(parents=True, exist_ok=True)

    skipped = 0
    try:
        if kind == "zip":
            files, dirs, skipped = _zip_extract(archive, dest)
        elif kind == "tar":
            files, dirs, skipped = _tar_extract(archive, dest)
        elif kind == "gz":
            target = _archive_target(dest, _strip_archive_suffix(archive.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with gzip.open(archive, "rb") as source, target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ToolError("Распакованный файл превысил лимит 4 ГБ.")
                    output.write(chunk)
            files, dirs = 1, 0
        else:
            binary = _seven_zip()
            if not binary:
                raise ToolError(
                    "Для 7z/rar нужен 7-Zip: установи его или распакуй архив вручную. "
                    "zip и tar распаковываются без внешних программ."
                )
            result = subprocess.run(
                [binary, "x", "-y", f"-o{dest}", str(archive)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            if result.returncode != 0:
                raise ToolError(
                    f"7-Zip вернул код {result.returncode}: "
                    f"{(result.stderr or result.stdout or '').strip()[:800]}"
                )
            files = sum(1 for item in dest.rglob("*") if item.is_file())
            dirs = sum(1 for item in dest.rglob("*") if item.is_dir())
    except ToolError:
        raise
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ToolError(f"Не удалось распаковать {_display(ctx, archive)}: {exc}") from exc

    ctx.audit.write(
        "extract_archive",
        path=str(archive),
        dest=str(dest),
        files=files,
        skipped=skipped,
        format=kind,
    )
    result = (
        f"Распаковано: {_display(ctx, dest)}\n"
        f"Формат: {kind}\n"
        f"Файлов: {files}, папок: {dirs}"
    )
    if skipped:
        result += f"\nПропущено ссылок/устройств: {skipped}"
    return result