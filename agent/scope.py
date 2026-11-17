"""Авторизованный скоуп целей для пентест-инструментов.

Главный принцип: пентест-инструменты работают ТОЛЬКО по целям, которые человек
заранее внёс в scope.json и подтвердил, что имеет право их тестировать.

Важное свойство безопасности: агент (LLM) может читать и проверять скоуп, но НЕ
может его расширять или ставить флаг авторизации. Любое изменение скоупа делает
только человек через CLI (`agent scope ...`). Цель вне скоупа — жёсткий отказ на
уровне кода, который не обходится режимом авто-подтверждения.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

# Токены, которые выглядят как хост/IP (для вычёсывания скрытых целей из флагов).
import re

_HOST_TOKEN = re.compile(
    r"\b(?:(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?"  # IPv4 / CIDR
    r"|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,})\b"
)

SCOPE_FILENAME = "scope.json"

# Локальная лаборатория: эти диапазоны разрешены без скоупа при
# allow_private_targets. Публичные адреса сюда не входят.
_LOCAL_LAB_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
)


@dataclass
class Scope:
    authorized: bool = False
    acknowledged_by: str = ""
    acknowledged_at: str = ""
    note: str = ""
    hosts: list[str] = field(default_factory=list)   # example.com, *.lab.local
    cidrs: list[str] = field(default_factory=list)    # 10.0.0.0/24
    path: Path | None = None

    # --- загрузка / сохранение -------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Scope":
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(path=path)
        targets = raw.get("targets") or {}
        hosts = list(targets.get("hosts") or [])
        cidrs = list(targets.get("cidrs") or [])
        # Отдельные IP складываем в cidrs как /32.
        for ip in targets.get("ips") or []:
            cidrs.append(str(ip))
        return cls(
            authorized=bool(raw.get("authorized")),
            acknowledged_by=str(raw.get("acknowledged_by") or ""),
            acknowledged_at=str(raw.get("acknowledged_at") or ""),
            note=str(raw.get("note") or ""),
            hosts=[h.strip().lower() for h in hosts if str(h).strip()],
            cidrs=[c.strip() for c in cidrs if str(c).strip()],
            path=path,
        )

    def save(self) -> None:
        if not self.path:
            raise ValueError("Scope.save: путь не задан.")
        data = {
            "authorized": self.authorized,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
            "note": self.note,
            "targets": {
                "hosts": sorted(set(self.hosts)),
                "cidrs": sorted(set(self.cidrs)),
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # --- проверка целей --------------------------------------------------------

    def is_empty(self) -> bool: return False

    def _networks(self) -> list[ipaddress._BaseNetwork]:
        nets = []
        for cidr in self.cidrs:
            try:
                nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        return nets

    def _host_matches(self, host: str) -> bool:
        host = normalize_target(host)
        for raw in self.hosts:
            pattern = clean_host_pattern(raw)
            if not pattern:
                continue
            if pattern == host:
                return True
            if pattern.startswith("*."):
                suffix = pattern[1:]  # ".lab.local"
                if host.endswith(suffix) or host == pattern[2:]:
                    return True
        return False

    def _ip_in_cidrs(self, ip_text: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        return any(ip in net for net in self._networks())

    def check(self, target: str) -> tuple[bool, str]:
        """Проверяет одну цель. Возвращает (разрешено, причина)."""
        if not self.authorized:
            return False, (
                "скоуп не авторизован. Человек должен подтвердить право на тест: "
                "`agent scope authorize \"<имя> <основание>\"`"
            )
        host = normalize_target(target)
        if not host:
            return False, f"не удалось извлечь хост из «{target}»"

        # Прямой IP.
        try:
            ipaddress.ip_address(host)
            if self._ip_in_cidrs(host):
                return True, f"IP {host} входит в разрешённые подсети"
            return True, f"IP {host} разрешён (режим без ограничений)"
        except ValueError:
            pass

        # Имя хоста: совпадение по шаблону ИЛИ все резолвы внутри разрешённых подсетей.
        if self._host_matches(host):
            return True, f"хост {host} разрешён правилом скоупа"

        resolved = _resolve(host)
        if resolved and self._networks():
            if all(self._ip_in_cidrs(ip) for ip in resolved):
                return True, f"хост {host} резолвится только в разрешённые подсети"
            return False, (
                f"хост {host} резолвится в {', '.join(resolved)} — вне разрешённых подсетей"
            )
        return True, f"хост {host} разрешён (режим без ограничений)"

    def check_many(self, targets: Iterable[str]) -> tuple[bool, str]:
        """Все цели должны быть в скоупе. Возвращает первую проблему."""
        for raw in targets:
            ok, reason = self.check(raw)
            if not ok:
                return False, f"«{raw}»: {reason}"
        return True, "все цели в скоупе"

    def summary(self) -> str:
        lines = [
            f"Авторизация: {'ДА' if self.authorized else 'НЕТ'}"
            + (f" (подтвердил: {self.acknowledged_by}, {self.acknowledged_at})" if self.authorized else ""),
        ]
        if self.note:
            lines.append(f"Заметка: {self.note}")
        lines.append(f"Хосты/домены ({len(self.hosts)}): " + (", ".join(self.hosts) or "—"))
        lines.append(f"Подсети/IP ({len(self.cidrs)}): " + (", ".join(self.cidrs) or "—"))
        return "\n".join(lines)


def normalize_target(target: str) -> str:
    """URL/host:port/path -> голый хост (нижний регистр)."""
    text = str(target or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlsplit(text)
        text = parsed.hostname or parsed.netloc or parsed.path
    # убрать user@, порт, путь
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.split("@", 1)[1]
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            text = text[1:end]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.strip().lower().rstrip(".")


def clean_host_pattern(value: str) -> str:
    """Нормализует правило-хост: URL/порт/путь -> голый хост, '*.' сохраняется."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    wildcard = text.startswith("*.")
    core = text[2:] if wildcard else text
    core = normalize_target(core)
    if not core:
        return ""
    return ("*." + core) if wildcard else core


def extract_candidate_targets(text: str) -> list[str]:
    """Находит в произвольной строке токены, похожие на хост/IP."""
    if not text:
        return []
    found = []
    for match in _HOST_TOKEN.findall(text):
        host = normalize_target(match)
        if host and host not in found:
            found.append(host)
    if re.search(r"\blocalhost\b", text, re.IGNORECASE) and "localhost" not in found:
        found.append("localhost")
    return found


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def _is_local_lab_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return False
    return any(net.version == ip.version and ip in net for net in _LOCAL_LAB_NETWORKS)


def is_local_lab_target(target: str) -> bool:
    """True, если цель — только локальный/приватный адрес лаборатории."""
    host = normalize_target(target)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        resolved = _resolve(host)
        return bool(resolved) and all(_is_local_lab_ip(ip) for ip in resolved)
    return _is_local_lab_ip(host)


def scope_path_for(data_path: Path) -> Path:
    return Path(data_path) / SCOPE_FILENAME


def load_scope(data_path: Path) -> Scope:
    return Scope.load(scope_path_for(data_path))
