from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .utils import now_iso, parse_front_matter, read_text, slugify, write_text


class Memory:
    """Долговременная память: простые факты в JSONL."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("text"):
                records.append(record)
        return records

    def add(self, text: str, tags: list[str] | None = None) -> dict:
        record = {
            "id": uuid.uuid4().hex[:8],
            "ts": now_iso(),
            "text": str(text).strip(),
            "tags": [str(tag) for tag in (tags or [])],
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def recent(self, limit: int = 10) -> list[dict]:
        records = self._load()
        return records[-limit:] if limit > 0 else []

    def all(self) -> list[dict]:
        return self._load()

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query_lower = query.strip().lower()
        if not query_lower:
            return []
        tokens = [token for token in re.findall(r"\w+", query_lower) if len(token) > 1]
        scored: list[tuple[int, dict]] = []
        for record in self._load():
            text = str(record.get("text", "")).lower()
            tags = " ".join(str(tag) for tag in record.get("tags", [])).lower()
            haystack = f"{text} {tags}"
            score = 0
            if query_lower in haystack:
                score += 5
            for token in tokens:
                if token in haystack:
                    score += 1
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].get("ts", "")), reverse=True)
        return [record for _, record in scored[:limit]]

    def forget(self, record_id: str) -> bool:
        records = self._load()
        kept = [record for record in records if record.get("id") != record_id]
        if len(kept) == len(records):
            return False
        self._rewrite(kept)
        return True

    def clear(self) -> int:
        records = self._load()
        self._rewrite([])
        return len(records)

    def _rewrite(self, records: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp.replace(self.path)


class Lessons:
    """Уроки самообучения: проблема -> проверенное решение.

    В отличие от памяти (факты) и навыков (методики), урок фиксирует
    конкретную ошибку и способ её исправления, чтобы агент не повторял
    её в следующий раз.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("problem") and record.get("solution"):
                records.append(record)
        return records

    def _rewrite(self, records: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def add(
        self,
        problem: str,
        solution: str,
        tags: list[str] | None = None,
        source: str = "",
    ) -> tuple[dict, bool]:
        problem = str(problem).strip()
        solution = str(solution).strip()
        if not problem or not solution:
            raise ValueError("Нужны problem и solution.")
        records = self._load()
        key = problem.lower()
        for record in records:
            if str(record.get("problem", "")).lower() == key:
                record.update(
                    {
                        "ts": now_iso(),
                        "solution": solution,
                        "tags": [str(tag) for tag in (tags or record.get("tags") or [])],
                        "source": str(source or record.get("source") or ""),
                    }
                )
                self._rewrite(records)
                return record, False
        record = {
            "id": uuid.uuid4().hex[:8],
            "ts": now_iso(),
            "problem": problem,
            "solution": solution,
            "tags": [str(tag) for tag in (tags or [])],
            "source": str(source or ""),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record, True

    def recent(self, limit: int = 10) -> list[dict]:
        records = self._load()
        return records[-limit:] if limit > 0 else []

    def all(self) -> list[dict]:
        return self._load()

    def count(self) -> int:
        return len(self._load())

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query_lower = str(query).strip().lower()
        if not query_lower:
            return []
        tokens = [token for token in re.findall(r"\w+", query_lower) if len(token) > 1]
        scored: list[tuple[int, dict]] = []
        for record in self._load():
            haystack = " ".join(
                str(record.get(field, "")).lower()
                for field in ("problem", "solution", "source", "tags")
            )
            score = 0
            if query_lower in haystack:
                score += 5
            for token in tokens:
                if token in haystack:
                    score += 1
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].get("ts", "")), reverse=True)
        return [record for _, record in scored[:limit]]

    def forget(self, record_id: str) -> bool:
        records = self._load()
        kept = [record for record in records if record.get("id") != record_id]
        if len(kept) == len(records):
            return False
        self._rewrite(kept)
        return True


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path


class Skills:
    """Навыки — markdown-файлы с инструкциями, которые агент подгружает по мере надобности."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[SkillMeta]:
        skills: list[SkillMeta] = []
        for path in sorted(self.directory.glob("*.md")):
            meta, body = parse_front_matter(read_text(path))
            name = meta.get("name") or path.stem
            description = meta.get("description") or _first_heading(body) or "без описания"
            skills.append(SkillMeta(name=name, description=description, path=path))
        return skills

    def get(self, name: str) -> tuple[SkillMeta, str] | None:
        wanted = name.strip().lower()
        if wanted.endswith(".md"):
            wanted = wanted[:-3]
        for meta in self.list():
            if meta.name.lower() == wanted or meta.path.stem.lower() == slugify(wanted):
                return meta, read_text(meta.path)
        return None

    def save(self, name: str, description: str, body: str, overwrite: bool = False) -> tuple[SkillMeta, Path]:
        slug = slugify(name)
        path = self.directory / f"{slug}.md"
        if path.exists() and not overwrite:
            raise FileExistsError(f"Навык уже существует: {path.name}. Укажи overwrite=true, чтобы заменить.")
        text = (
            "---\n"
            f"name: {slug}\n"
            f"description: {description.strip()}\n"
            "---\n\n"
            f"{body.strip()}\n"
        )
        write_text(path, text)
        return SkillMeta(name=slug, description=description.strip(), path=path), path

    def delete(self, name: str) -> bool:
        found = self.get(name)
        if not found:
            return False
        found[0].path.unlink(missing_ok=True)
        return True


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""