from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path

DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat\s+[a-z]:", "форматирование диска"),
    (r"\bdiskpart\b", "управление разделами диска"),
    (r"\bmkfs\b", "форматирование файловой системы"),
    (r"\bdd\s+if=", "запись образа на диск"),
    (r"\brm\s+-[a-z]*rf?\b", "рекурсивное удаление"),
    (r"\bdel\b.*(/s|/f|/q)\b", "удаление файлов с ключами"),
    (r"\berase\b", "удаление файлов"),
    (r"\bshutdown\b", "выключение компьютера"),
    (r"\brestart-computer\b", "перезагрузка компьютера"),
    (r"\bstop-computer\b", "выключение компьютера"),
    (r"\breg\s+(add|delete)\b", "изменение реестра"),
    (r"\bbcdedit\b", "изменение загрузчика Windows"),
    (r"\bvssadmin\b", "управление теневыми копиями"),
    (r"\bnet\s+user\b", "изменение пользователей"),
    (r"\bnet\s+localgroup\b", "изменение групп пользователей"),
    (r"\btakeown\b", "изменение владельца файлов"),
    (r"\bicacls\b.*/grant", "изменение прав доступа"),
    (r"\bremove-item\b.*-recurse", "рекурсивное удаление"),
    (r"\bset-executionpolicy\b", "изменение политики PowerShell"),
    (r"\binvoke-expression\b", "выполнение произвольного кода"),
    (r"\biex\b", "выполнение произвольного кода"),
    (r"\bwmic\b", "устаревшее управление системой"),
    (r"\bschtasks\b", "изменение планировщика задач"),
    (r"\bcipher\s+/w", "затирание свободного места"),
    (r"\bcurl\b.*\|\s*(bash|sh|powershell|cmd)", "запуск скачанного кода"),
    (r"\biwr\b.*\|\s*(iex|bash|sh)", "запуск скачанного кода"),
    (r"\bgit\s+push\b.*--force", "принудительная отправка в git"),
]

_COMPILED_DANGEROUS = [
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in DANGEROUS_PATTERNS
]


def dangerous_reason(command: str) -> str | None:
    for regex, reason in _COMPILED_DANGEROUS:
        if regex.search(command):
            return reason
    return None


# Наступательные/сетевые инструменты: если они есть в произвольной shell-команде,
# цель обязана быть в авторизованном скоупе (см. scope.py). Это закрывает обход
# скоупа через run_shell.
OFFENSIVE_BINARIES: set[str] = {
    "nmap", "masscan", "rustscan", "zmap", "unicornscan", "naabu",
    "nuclei", "nikto", "wpscan", "joomscan", "whatweb", "wafw00f",
    "ffuf", "gobuster", "dirb", "dirsearch", "feroxbuster", "wfuzz", "katana",
    "sqlmap", "nosqlmap", "commix", "xsstrike", "dalfox", "gau", "waybackurls", "paramspider",
    "hydra", "medusa", "ncrack", "patator", "crackmapexec", "netexec", "cme",
    "subfinder", "amass", "dnsx", "dnsrecon", "fierce", "theharvester",
    "metasploit", "msfconsole", "msfvenom", "responder", "enum4linux",
    "testssl", "sslscan", "sslyze", "openvas", "nessus",
}

_OFFENSIVE_RE = re.compile(
    r"(?<![\w.-])(" + "|".join(sorted(map(re.escape, OFFENSIVE_BINARIES), key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)


def offensive_tool_in(command: str) -> str | None:
    """Возвращает имя наступательного инструмента, если он есть в команде."""
    match = _OFFENSIVE_RE.search(command or "")
    return match.group(1).lower() if match else None


def is_private_host(host: str) -> bool:
    """Защита от SSRF: запрещаем обращения к локальной сети из web_fetch."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def is_inside(root: Path, candidate: Path) -> bool:
    root_key = os.path.normcase(str(Path(root).resolve()))
    candidate_key = os.path.normcase(str(Path(candidate).resolve()))
    return candidate_key == root_key or candidate_key.startswith(root_key + os.sep)