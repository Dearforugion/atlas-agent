"""Сетевая проверка портов без nmap.

TCP connect-скан через стандартную библиотеку. Публичные цели — только
из авторизованного скоупа; локальная лаборатория — без скоупа.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from ..errors import ToolError
from ..paths import ensure_path, gate_if_outside
from ..safety import is_private_host
from ..scope import is_local_lab_target, load_scope, normalize_target
from ..utils import write_text
from . import tool

USER_AGENT = "ATLAS/1.0 (+local security audit)"
DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
DNS_TYPES = {
    "A": 1,
    "NS": 2,
    "CNAME": 5,
    "SOA": 6,
    "PTR": 12,
    "MX": 15,
    "TXT": 16,
    "AAAA": 28,
    "SRV": 33,
    "CAA": 257,
}
DEFAULT_DNS_TYPES = ["A", "AAAA", "MX", "NS", "TXT"]
DNS_STATUS = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN (домен не существует)",
    4: "NOTIMP",
    5: "REFUSED",
}

COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445,
    993, 995, 1433, 1723, 3306, 3389, 5432, 5900, 6379, 8080,
    8443, 9000, 9200, 27017,
]


def _scope_reason(ctx, target: str) -> str:
    host = normalize_target(target)
    if not host:
        raise ToolError("Не удалось определить хост цели.")
    if ctx.settings.allow_private_targets and is_local_lab_target(host):
        return "локальная лаборатория"
    scope = load_scope(ctx.settings.data_path)
    ok, reason = scope.check(host)
    if not ok:
        ctx.audit.write("scope_block", tool="port_scan", target=host, reason=reason)
        raise ToolError(
            f"ОТКАЗАНО скоупом: {reason}.\n"
            f"Для публичной цели добавь её в скоуп и авторизуй:\n"
            f"  agent scope allow {host} \"<кто и основание>\"\n"
            f"или:\n"
            f"  agent scope add-host {host}\n"
            f"  agent scope authorize \"<кто и основание>\""
        )
    return reason


def _parse_ports(raw) -> list[int]:
    if raw is None or str(raw).strip() == "":
        return list(COMMON_PORTS)
    ports: list[int] = []
    for part in re.split(r"[,\s]+", str(raw).strip()):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ToolError(f"Неверный диапазон портов: {part}") from exc
            if start < 1 or end > 65535 or start > end:
                raise ToolError(f"Неверный диапазон портов: {part}")
            ports.extend(range(start, end + 1))
        else:
            try:
                port = int(part)
            except ValueError as exc:
                raise ToolError(f"Неверный порт: {part}") from exc
            if not 1 <= port <= 65535:
                raise ToolError(f"Порт вне диапазона 1-65535: {port}")
            ports.append(port)
    unique = sorted(set(ports))
    if len(unique) > 2000:
        raise ToolError("Слишком много портов: максимум 2000 за один запуск.")
    return unique


def _resolve_target(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"Не удалось разрешить {host}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips[:8]


def _check_port(ip: str, port: int, timeout: float) -> tuple[int, bool, str]:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            try:
                service = socket.getservbyport(port, "tcp")
            except OSError:
                service = ""
            return port, True, service
    except (TimeoutError, ConnectionRefusedError, OSError):
        return port, False, ""


@tool(
    "port_scan",
    "TCP-сканирование портов без nmap (stdlib). Публичные цели — только из авторизованного скоупа; "
    "локальные/приватные — без скоупа. Сохраняет отчёт в workspace/reports/.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "IP или домен"},
            "ports": {"type": "string", "description": "Порты: '22,80,443' или '1-1024'. По умолчанию частые."},
            "timeout": {"type": "number", "description": "Таймаут на порт, сек (по умолчанию 1.0)"},
            "workers": {"type": "integer", "description": "Потоков (по умолчанию 100, максимум 200)"},
        },
        "required": ["target"],
    },
)
def port_scan(args: dict, ctx) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("Не указана цель.")
    host = normalize_target(target)
    reason = _scope_reason(ctx, host)
    ports = _parse_ports(args.get("ports"))
    timeout = max(0.2, min(5.0, float(args.get("timeout") or 1.0)))
    workers = max(1, min(200, int(args.get("workers") or 100)))
    ips = _resolve_target(host)
    if not ips:
        raise ToolError(f"Не удалось получить IP для {host}.")

    started = datetime.now()
    results: list[tuple[int, bool, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_check_port, ip, port, timeout)
            for ip in ips
            for port in ports
        ]
        for future in as_completed(futures):
            results.append(future.result())

    # Если IP несколько, порт считается открытым, если он открыт хотя бы на одном.
    open_ports = sorted({port for port, is_open, _ in results if is_open})
    services = {}
    for port, is_open, service in results:
        if is_open and service:
            services.setdefault(port, service)

    lines = [
        f"# Скан портов: {host}",
        "",
        f"- Дата: {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Цель: {target}",
        f"- IP: {', '.join(ips)}",
        f"- Скоуп: {reason}",
        f"- Проверено портов: {len(ports)} (IP: {len(ips)})",
        "",
        "## Открытые порты",
    ]
    if open_ports:
        lines.extend(["| Порт | Сервис |", "|---|---|"])
        lines.extend([f"| {port} | {services.get(port, '—')} |" for port in open_ports])
    else:
        lines.append("Открытых портов не найдено.")

    reports = Path(ctx.workspace) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    safe_host = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host)[:80] or "target"
    report_path = reports / f"port_scan_{safe_host}_{started.strftime('%Y%m%d-%H%M%S')}.md"
    write_text(report_path, "\n".join(lines) + "\n")

    ctx.audit.write("port_scan", target=host, ips=ips, ports=len(ports), open=open_ports)
    summary = [
        f"Скан {host} завершён. IP: {', '.join(ips)}",
        f"Скоуп: {reason}",
        f"Открытые порты: {', '.join(map(str, open_ports)) or 'нет'}",
        f"Отчёт: {report_path}",
    ]
    return "\n".join(summary)


def _parse_dns_types(raw) -> list[str]:
    if raw is None or str(raw).strip() == "":
        return list(DEFAULT_DNS_TYPES)
    result: list[str] = []
    for part in re.split(r"[,\s]+", str(raw).strip().upper()):
        if not part:
            continue
        if part == "ALL":
            return list(DNS_TYPES)
        if part not in DNS_TYPES:
            raise ToolError(f"Неизвестный тип DNS-записи: {part}. Доступно: {', '.join(DNS_TYPES)}")
        if part not in result:
            result.append(part)
    if not result:
        return list(DEFAULT_DNS_TYPES)
    return result[:12]


def _doh_query(name: str, record_type: str, timeout: int) -> tuple[dict | None, str]:
    query = urllib.parse.urlencode(
        {"name": name, "type": record_type, "ct": "application/dns-json"}
    )
    last_error = ""
    for endpoint in DOH_ENDPOINTS:
        request = urllib.request.Request(
            f"{endpoint}?{query}",
            headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(2_000_000).decode("utf-8", "replace")
            return json.loads(payload), ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


@tool(
    "dns_lookup",
    "DNS-разведка через DNS-over-HTTPS: A, AAAA, MX, NS, TXT, CNAME, SOA, PTR, SRV, CAA. "
    "Публичные цели — только из авторизованного скоупа; локальные — без скоупа.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Домен или IP"},
            "types": {
                "type": "string",
                "description": "Типы через запятую, например 'A,AAAA,MX,TXT'. По умолчанию A,AAAA,MX,NS,TXT",
            },
            "timeout": {"type": "integer", "description": "Таймаут запроса, сек (по умолчанию 10)"},
        },
        "required": ["target"],
    },
)
def dns_lookup(args: dict, ctx) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("Не указана цель.")
    host = normalize_target(target)
    if not host:
        raise ToolError("Не удалось определить домен или IP.")
    reason = _scope_reason(ctx, host)
    timeout = max(3, min(30, int(args.get("timeout") or 10)))

    query_name = host
    try:
        ip = ipaddress.ip_address(host)
        query_name = ip.reverse_pointer
        default_types = ["PTR"]
    except ValueError:
        ip = None
        default_types = list(DEFAULT_DNS_TYPES)

    raw_types = args.get("types")
    types = _parse_dns_types(raw_types) if raw_types else default_types
    if ip is not None:
        types = ["PTR" if item in ("A", "AAAA") else item for item in types]

    lines = [f"DNS: {host}", f"Скоуп: {reason}", f"Запрос: {query_name}", ""]
    for record_type in types:
        payload, error = _doh_query(query_name, record_type, timeout)
        if payload is None:
            lines.append(f"{record_type}: ошибка запроса — {error}")
            continue
        status = int(payload.get("Status", 0))
        answers = payload.get("Answer") or []
        if status != 0:
            lines.append(f"{record_type}: {DNS_STATUS.get(status, f'статус {status}')}")
            continue
        if not answers:
            lines.append(f"{record_type}: нет записей")
            continue
        lines.append(f"{record_type}:")
        for answer in answers:
            name = str(answer.get("name") or "").rstrip(".")
            ttl = answer.get("TTL", "?")
            data = str(answer.get("data") or "").strip('"')
            lines.append(f"  {name}  TTL {ttl}  {data}")
    ctx.audit.write("dns_lookup", target=host, types=types)
    return "\n".join(lines)


def _rdap_request(host: str, timeout: int) -> tuple[dict | None, str, str]:
    url = f"https://rdap.org/domain/{urllib.parse.quote(host)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rdap+json, application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4_000_000).decode("utf-8", "replace")
            return json.loads(raw), response.geturl(), ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, url, "домен не найден в RDAP"
        return None, url, f"HTTP {exc.code}"
    except Exception as exc:
        return None, url, f"{type(exc).__name__}: {exc}"


def _vcard_value(entity: dict, key: str) -> str:
    vcard = entity.get("vcardArray")
    if not isinstance(vcard, list) or len(vcard) < 2 or not isinstance(vcard[1], list):
        return ""
    for item in vcard[1]:
        if isinstance(item, list) and len(item) >= 4 and item[0] == key:
            value = item[3]
            if isinstance(value, list):
                return ", ".join(str(part) for part in value)
            return str(value)
    return ""


@tool(
    "whois_lookup",
    "WHOIS/RDAP-справка о домене: регистратор, даты, статусы, NS и DNSSEC. "
    "Публичные цели — только из авторизованного скоупа; локальные — без скоупа.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Домен, например example.com"},
            "timeout": {"type": "integer", "description": "Таймаут запроса, сек (по умолчанию 15)"},
        },
        "required": ["target"],
    },
)
def whois_lookup(args: dict, ctx) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("Не указана цель.")
    host = normalize_target(target)
    if not host:
        raise ToolError("Не удалось определить домен.")
    try:
        ipaddress.ip_address(host)
        raise ToolError("WHOIS/RDAP работает с доменами, а не с IP.")
    except ValueError:
        pass
    reason = _scope_reason(ctx, host)
    timeout = max(5, min(60, int(args.get("timeout") or 15)))

    data, final_url, error = _rdap_request(host, timeout)
    if data is None:
        raise ToolError(f"Не удалось получить RDAP для {host}: {error}")
    ctx.audit.write("whois_lookup", target=host, url=final_url)

    lines = [f"WHOIS/RDAP: {host}", f"Скоуп: {reason}", f"Источник: {final_url}", ""]

    names = [str(data.get("ldhName") or ""), str(data.get("unicodeName") or "")]
    names = [name for name in names if name]
    if names:
        lines.append(f"Домен: {', '.join(dict.fromkeys(names))}")

    statuses = data.get("status") or []
    if statuses:
        lines.append("Статусы: " + ", ".join(str(item) for item in statuses))

    events = data.get("events") or []
    labels = {
        "registration": "Регистрация",
        "expiration": "Истекает",
        "last changed": "Изменён",
        "last update of RDAP database": "База RDAP обновлена",
    }
    for event in events:
        action = str(event.get("eventAction") or "")
        date = str(event.get("eventDate") or "")
        if action and date:
            lines.append(f"{labels.get(action, action)}: {date}")

    registrar = ""
    abuse_email = ""
    for entity in data.get("entities") or []:
        roles = [str(role) for role in entity.get("roles") or []]
        if "registrar" in roles and not registrar:
            registrar = _vcard_value(entity, "fn")
            for child in entity.get("entities") or []:
                child_roles = [str(role) for role in child.get("roles") or []]
                if "abuse" in child_roles and not abuse_email:
                    abuse_email = _vcard_value(child, "email")
        if "abuse" in roles and not abuse_email:
            abuse_email = _vcard_value(entity, "email")
    if registrar:
        lines.append(f"Регистратор: {registrar}")
    if abuse_email:
        lines.append(f"Abuse-контакт: {abuse_email}")

    nameservers = []
    for server in data.get("nameservers") or []:
        name = str(server.get("ldhName") or "").rstrip(".")
        if name:
            nameservers.append(name)
    if nameservers:
        lines.append("NS: " + ", ".join(nameservers))

    secure = data.get("secureDNS") or {}
    if secure:
        signed = secure.get("delegationSigned")
        lines.append(f"DNSSEC: {'подписан' if signed else 'не подписан'}")

    return "\n".join(lines)


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} Б"
        value /= 1024
    return f"{num_bytes} Б"


def _safe_filename(name: str) -> str:
    name = urllib.parse.unquote(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if name in ("", ".", ".."):
        return "download.bin"
    # Ведущая точка сохраняется: агенту иногда нужны .env и подобные файлы.
    return name[:120]


@tool(
    "download_file",
    "Скачать файл по http(s)-ссылке в рабочую папку. Подходит для установщиков, "
    "словарей, бинарников и датасетов: проверяет размер, считает SHA-256 и не "
    "оставляет недокачанных файлов.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полная http(s)-ссылка на файл"},
            "path": {
                "type": "string",
                "description": "Куда сохранить относительно рабочей папки. Если не указано — имя из ссылки",
            },
            "overwrite": {"type": "boolean", "description": "Перезаписать существующий файл"},
            "max_mb": {
                "type": "integer",
                "description": "Максимальный размер в МБ (по умолчанию 500, максимум 4096)",
            },
        },
        "required": ["url"],
    },
)
def download_file(args: dict, ctx) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolError("Пустая ссылка.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Разрешены только http/https-ссылки.")
    if not parsed.hostname:
        raise ToolError("В ссылке нет имени хоста.")

    max_mb = max(1, min(4096, int(args.get("max_mb") or 500)))
    max_bytes = max_mb * 1024 * 1024

    raw_path = str(args.get("path") or "").strip()
    if raw_path:
        target = ensure_path(ctx, raw_path)
        if target.is_dir() or raw_path.endswith(("/", "\\")):
            target = target / _safe_filename(Path(parsed.path).name)
    else:
        target = ensure_path(ctx, _safe_filename(Path(parsed.path).name))

    gate_if_outside(ctx, target, "Скачивание файла")
    if target.exists() and not bool(args.get("overwrite")):
        raise ToolError(
            f"Файл уже существует: {target}. Укажи overwrite=true, чтобы заменить."
        )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            final_url = response.geturl()
            final = urllib.parse.urlparse(final_url)
            if final.scheme not in ("http", "https") or not final.hostname:
                raise ToolError(f"Редирект на недопустимый адрес: {final_url}")
            if is_private_host(final.hostname) and not ctx.settings.allow_private_web:
                raise ToolError(
                    f"Редирект в локальную сеть запрещён: {final_url}. "
                    "Включи allow_private_web в config.json, если это лабораторный сервер."
                )
            length_header = response.headers.get("Content-Length")
            if length_header and length_header.isdigit():
                declared = int(length_header)
                if declared > max_bytes:
                    raise ToolError(
                        f"Файл слишком большой: {_human_size(declared)} "
                        f"(лимит {max_mb} МБ). Увеличь max_mb."
                    )
            with temp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ToolError(
                            f"Файл превысил лимит {max_mb} МБ — скачивание остановлено."
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except ToolError:
        temp.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        temp.unlink(missing_ok=True)
        raise ToolError(f"Сайт вернул HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        temp.unlink(missing_ok=True)
        raise ToolError(f"Не удалось скачать {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        temp.unlink(missing_ok=True)
        raise ToolError(f"Таймаут при скачивании {url}.") from exc
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ToolError(f"Ошибка записи файла {target}: {exc}") from exc

    if total == 0:
        temp.unlink(missing_ok=True)
        raise ToolError(f"Сервер вернул пустой файл: {url}")

    temp.replace(target)
    sha256 = digest.hexdigest()
    ctx.audit.write(
        "download_file",
        url=final_url,
        path=str(target),
        bytes=total,
        sha256=sha256,
    )
    return (
        f"Скачано: {target}\n"
        f"URL: {final_url}\n"
        f"Размер: {_human_size(total)}\n"
        f"SHA-256: {sha256}"
    )