"""Статистика длительности шагов для оценки оставшегося времени.

ATLAS запоминает, сколько в среднем занимают ответы модели и отдельные
инструменты, и использует эти значения для прогресс-бара и оценки «ост. ~Nс».
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_ALPHA = 0.35


class Metrics:
    """Скользящие средние по модели и инструментам, с сохранением на диск."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, dict]] = {"llm": {}, "tool": {}}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for kind in ("llm", "tool"):
            section = raw.get(kind)
            if not isinstance(section, dict):
                continue
            for key, entry in section.items():
                if not isinstance(entry, dict):
                    continue
                average = entry.get("avg")
                if not isinstance(average, (int, float)):
                    continue
                self.data[kind][str(key)] = {
                    "avg": float(average),
                    "count": int(entry.get("count") or 0),
                    "last": float(entry.get("last") or 0),
                }

    def average(self, kind: str, key: str, default: float = 0.0) -> float:
        entry = self.data.get(kind, {}).get(key)
        if not entry:
            return float(default)
        return max(0.0, float(entry.get("avg") or default))

    def observe(self, kind: str, key: str, seconds: float) -> None:
        if seconds <= 0.2:
            return
        key = str(key or "unknown")
        with self._lock:
            section = self.data.setdefault(kind, {})
            entry = section.get(key)
            if entry:
                entry["avg"] = round(entry["avg"] * (1 - _ALPHA) + seconds * _ALPHA, 3)
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last"] = round(seconds, 3)
            else:
                section[key] = {
                    "avg": round(seconds, 3),
                    "count": 1,
                    "last": round(seconds, 3),
                }
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            pass


def default_llm_seconds(base_url: str) -> float:
    """Первая оценка, пока нет статистики: локальная модель обычно медленнее."""
    url = str(base_url or "").lower()
    if any(host in url for host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")):
        return 35.0
    return 12.0


_TOOL_DEFAULTS = {
    "run_shell": 12.0,
    "run_python": 8.0,
    "web_fetch": 6.0,
    "web_search": 6.0,
    "site_audit": 30.0,
    "crawl_site": 40.0,
    "port_scan": 30.0,
    "dns_lookup": 5.0,
    "whois_lookup": 5.0,
    "tls_info": 5.0,
    "pentest_run": 60.0,
    "subagent": 90.0,
    "hash_file": 4.0,
}


def default_tool_seconds(name: str) -> float:
    return _TOOL_DEFAULTS.get(str(name), 5.0)


def format_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}с"
    minutes = int(seconds // 60)
    rest = int(round(seconds - minutes * 60))
    if rest >= 60:
        minutes += 1
        rest = 0
    return f"{minutes}м {rest:02d}с"
