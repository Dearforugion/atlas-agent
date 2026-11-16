"""Аудит сайта без внешних зависимостей.

Проверяет HTTP-заголовки, TLS-сертификат, DNS, технологии и типовые пути,
сохраняет Markdown-отчёт в workspace/reports/.
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from ..errors import ToolError
from ..scope import is_local_lab_target, load_scope, normalize_target
from ..utils import write_text
from . import tool

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_BODY = 1_500_000
MAX_PATH_BODY = 64_000
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".mp4", ".mp3",
    ".avi", ".mov", ".wav", ".exe", ".msi", ".dmg", ".iso",
}
SKIP_TOKENS = ("logout", "signout", "log-out", "delete", "remove", "destroy", "unsubscribe")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

COMMON_PATHS = [
    "/robots.txt",
    "/sitemap.xml",
    "/.well-known/security.txt",
    "/favicon.ico",
    "/login",
    "/admin",
    "/api",
    "/.git/HEAD",
    "/.env",
    "/server-status",
    "/wp-login.php",
    "/phpmyadmin/",
    "/actuator",
    "/swagger-ui/",
]

SECURITY_HEADERS = [
    "content-security-policy",
    "strict-transport-security",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
]

TECH_HEADERS = {
    "server": "Server",
    "x-powered-by": "X-Powered-By",
    "x-aspnet-version": "ASP.NET",
    "x-aspnetmvc-version": "ASP.NET MVC",
    "x-generator": "Generator",
    "x-drupal-cache": "Drupal",
    "x-drupal-dynamic-cache": "Drupal",
    "x-shopify-stage": "Shopify",
    "x-vercel-id": "Vercel",
    "cf-ray": "Cloudflare",
    "x-github-request-id": "GitHub",
}

TECH_PATTERNS = [
    (r"wp-content|wp-includes", "WordPress"),
    (r"joomla", "Joomla"),
    (r"drupal", "Drupal"),
    (r"__NEXT_DATA__|/_next/", "Next.js"),
    (r"__NUXT__|/_nuxt/", "Nuxt"),
    (r"react(?:\.min)?\.js|data-reactroot", "React"),
    (r"vue(?:\.min)?\.js|data-v-", "Vue"),
    (r"angular(?:\.min)?\.js|ng-version", "Angular"),
    (r"bootstrap(?:\.min)?\.(?:css|js)", "Bootstrap"),
    (r"jquery(?:\.min)?\.js", "jQuery"),
    (r"laravel", "Laravel"),
    (r"django", "Django"),
    (r"csrfmiddlewaretoken", "Django"),
    (r"PHPSESSID", "PHP"),
    (r"JSESSIONID", "Java"),
    (r"ASP\.NET_SessionId", "ASP.NET"),
]

COOKIE_TECH = {
    "phpsessid": "PHP",
    "jsessionid": "Java",
    "asp.net_sessionid": "ASP.NET",
    "laravel_session": "Laravel",
    "wordpress_test_cookie": "WordPress",
    "wp-settings": "WordPress",
}


def _scope_reason(ctx, target: str) -> str:
    host = normalize_target(target)
    if not host:
        raise ToolError("Не удалось определить хост цели.")
    if ctx.settings.allow_private_targets and is_local_lab_target(host):
        return "локальная лаборатория"
    scope = load_scope(ctx.settings.data_path)
    ok, reason = scope.check(host)
    if not ok:
        ctx.audit.write("scope_block", tool="site_audit", target=host, reason=reason)
        raise ToolError(
            f"ОТКАЗАНО скоупом: {reason}.\n"
            f"Для публичной цели добавь её в скоуп и авторизуй:\n"
            f"  agent scope allow {host} \"<кто и основание>\"\n"
            f"или:\n"
            f"  agent scope add-host {host}\n"
            f"  agent scope authorize \"<кто и основание>\""
        )
    return reason


def _normalize_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ToolError("Пустой URL.")
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Поддерживаются только http/https.")
    if not parsed.hostname:
        raise ToolError("В URL нет хоста.")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _request(
    url: str,
    method: str = "GET",
    timeout: int = 15,
    max_body: int = MAX_BODY,
):
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response, response.read(max_body), None
    except urllib.error.HTTPError as exc:
        body = exc.read(max_body) if exc.fp else b""
        return exc, body, None
    except urllib.error.URLError as exc:
        return None, b"", str(exc.reason)
    except TimeoutError:
        return None, b"", "timeout"
    except Exception as exc:  # сеть бывает очень разной
        return None, b"", f"{type(exc).__name__}: {exc}"


def _decode_body(data: bytes, headers) -> str:
    charset = headers.get_content_charset() if headers else None
    for encoding in (charset, "utf-8", "cp1251"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not match:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()


def _technologies(headers, html: str, cookies: list[str]) -> list[str]:
    found: set[str] = set()
    for header, label in TECH_HEADERS.items():
        value = headers.get(header) if headers else None
        if value:
            found.add(f"{label}: {value}")
    for pattern, label in TECH_PATTERNS:
        if re.search(pattern, html, re.I):
            found.add(label)
    for cookie in cookies:
        name = cookie.split("=", 1)[0].strip().lower()
        if name in COOKIE_TECH:
            found.add(COOKIE_TECH[name])
    return sorted(found)


def _security_headers(headers) -> list[tuple[str, str]]:
    result = []
    for name in SECURITY_HEADERS:
        value = headers.get(name) if headers else None
        result.append((name, value or ""))
    return result


def _dns_info(host: str) -> dict:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except socket.gaierror as exc:
        return {"ips": [], "reverse": "", "error": str(exc)}
    reverse = ""
    if ips:
        try:
            reverse = socket.gethostbyaddr(ips[0])[0]
        except (socket.herror, OSError):
            reverse = ""
    return {"ips": ips, "reverse": reverse}


def _cert_name(parts) -> str:
    values = []
    for item in parts or []:
        for key, value in item:
            values.append(f"{key}={value}")
    return ", ".join(values)


def _tls_info(host: str, port: int = 443, timeout: int = 10) -> dict:
    info: dict = {"host": host, "port": port, "verified": False}
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                info.update(
                    {
                        "version": tls.version() or "",
                        "cipher": tls.cipher()[0] if tls.cipher() else "",
                        "subject": _cert_name(cert.get("subject", [])),
                        "issuer": _cert_name(cert.get("issuer", [])),
                        "not_before": cert.get("notBefore", ""),
                        "not_after": cert.get("notAfter", ""),
                        "san": [value for key, value in cert.get("subjectAltName", []) if key == "DNS"],
                        "verified": True,
                    }
                )
                return info
    except ssl.SSLCertVerificationError as exc:
        info["error"] = str(exc)
        # Показываем сертификат даже без доверия к нему.
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
                    info.update(
                        {
                            "version": tls.version() or "",
                            "subject": _cert_name(cert.get("subject", [])),
                            "issuer": _cert_name(cert.get("issuer", [])),
                            "not_before": cert.get("notBefore", ""),
                            "not_after": cert.get("notAfter", ""),
                            "san": [value for key, value in cert.get("subjectAltName", []) if key == "DNS"],
                        }
                    )
        except Exception as exc2:
            info["error_retry"] = str(exc2)
        return info
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info


def _check_path(base_url: str, path: str, timeout: int) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(base_url)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    response, _, error = _request(url, timeout=min(timeout, 10), max_body=MAX_PATH_BODY)
    if response is None:
        return path, "ERR", error or ""
    status = str(getattr(response, "status", "?"))
    content_type = response.headers.get("Content-Type", "") if response.headers else ""
    return path, status, content_type


def _check_paths(base_url: str, timeout: int) -> list[tuple[str, str, str]]:
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_check_path, base_url, path, timeout) for path in COMMON_PATHS]
        return [future.result() for future in futures]


@tool(
    "site_audit",
    "Комплексный аудит сайта: HTTP-заголовки, TLS, DNS, технологии, типовые пути и Markdown-отчёт. "
    "Публичные цели — только из авторизованного скоупа; локальные/приватные — без скоупа.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL или домен, например https://example.com"},
            "deep": {"type": "boolean", "description": "Проверять типовые пути (по умолчанию true)"},
            "timeout": {"type": "integer", "description": "Таймаут запроса в секундах (по умолчанию 15)"},
        },
        "required": ["url"],
    },
)
def site_audit(args: dict, ctx) -> str:
    url = _normalize_url(args.get("url"))
    host = normalize_target(url)
    reason = _scope_reason(ctx, host)
    timeout = max(5, min(60, int(args.get("timeout") or 15)))
    deep = bool(args.get("deep", True))
    started = datetime.now()

    response, body, error = _request(url, timeout=timeout)
    warnings: list[str] = []
    if response is None and url.startswith("https://"):
        http_url = "http://" + url[len("https://"):]
        response, body, error = _request(http_url, timeout=timeout)
        if response is not None:
            warnings.append(f"HTTPS недоступен ({error}); использован HTTP.")
            url = http_url
    if response is None:
        raise ToolError(f"Не удалось подключиться к {url}: {error}")

    final_url = response.geturl() if hasattr(response, "geturl") else url
    headers = response.headers
    html = _decode_body(body, headers)
    title = _title(html)
    cookies = headers.get_all("Set-Cookie") or [] if headers else []
    tech = _technologies(headers, html, cookies)
    security = _security_headers(headers)
    missing = [name for name, value in security if not value]
    dns = _dns_info(host)
    tls = _tls_info(host, 443, timeout)
    path_rows = _check_paths(final_url, timeout) if deep else []

    lines = [
        f"# Аудит сайта: {host}",
        "",
        f"- Дата: {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Цель: {url}",
        f"- Финальный URL: {final_url}",
        f"- Статус: {getattr(response, 'status', '?')} {getattr(response, 'reason', '')}".rstrip(),
        f"- Скоуп: {reason}",
        f"- IP: {', '.join(dns.get('ips', [])) or '—'}",
        f"- Reverse DNS: {dns.get('reverse') or '—'}",
        f"- Заголовок: {title or '—'}",
        f"- Server: {headers.get('Server', '—') if headers else '—'}",
        "",
        "## Технологии",
    ]
    lines.extend([f"- {item}" for item in tech] or ["- не определены"])
    lines.extend(["", "## Заголовки безопасности", "| Заголовок | Значение |", "|---|---|"])
    lines.extend([f"| `{name}` | {value or '**нет**'} |" for name, value in security])
    lines.extend(["", "## TLS"])
    if tls.get("error") and not tls.get("subject"):
        lines.append(f"- Ошибка: {tls['error']}")
    else:
        lines.extend(
            [
                f"- Проверен: {'да' if tls.get('verified') else 'нет'}",
                f"- Версия: {tls.get('version') or '—'}",
                f"- Издатель: {tls.get('issuer') or '—'}",
                f"- Субъект: {tls.get('subject') or '—'}",
                f"- Действует с: {tls.get('not_before') or '—'}",
                f"- Действует до: {tls.get('not_after') or '—'}",
                f"- SAN: {', '.join(tls.get('san', [])) or '—'}",
            ]
        )
        if tls.get("error"):
            lines.append(f"- Предупреждение: {tls['error']}")

    if path_rows:
        lines.extend(["", "## Типовые пути", "| Путь | Статус | Content-Type |", "|---|---|---|"])
        for path, status, content_type in path_rows:
            lines.append(f"| `{path}` | {status} | {content_type} |")

    recommendations: list[str] = []
    if missing:
        recommendations.append("Добавить заголовки безопасности: " + ", ".join(missing))
    if url.startswith("http://"):
        recommendations.append("Включить HTTPS и перенаправление с HTTP.")
    if tls.get("error"):
        recommendations.append("Проверить TLS-сертификат: " + str(tls["error"]))
    for path, status, _ in path_rows:
        if path in ("/.git/HEAD", "/.env") and status == "200":
            recommendations.append(f"Критично: `{path}` доступен (200). Закрыть доступ.")
    if recommendations:
        lines.extend(["", "## Рекомендации"] + [f"- {item}" for item in recommendations])

    reports = Path(ctx.workspace) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    safe_host = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host)[:80] or "target"
    report_path = reports / f"site_audit_{safe_host}_{started.strftime('%Y%m%d-%H%M%S')}.md"
    write_text(report_path, "\n".join(lines) + "\n")

    ctx.audit.write(
        "site_audit",
        url=url,
        final_url=final_url,
        status=getattr(response, "status", None),
        report=str(report_path),
    )
    summary = [
        f"Аудит {host} завершён.",
        f"Статус: {getattr(response, 'status', '?')}, финальный URL: {final_url}",
        f"IP: {', '.join(dns.get('ips', [])) or '—'}; TLS: {'OK' if tls.get('verified') else 'проблема'}",
        f"Технологии: {', '.join(tech) or 'не определены'}",
        f"Нет заголовков безопасности: {', '.join(missing) or '—'}",
        f"Отчёт: {report_path}",
    ]
    if warnings:
        summary.append("Предупреждения: " + "; ".join(warnings))
    return "\n".join(summary)


@tool(
    "tls_info",
    "Показать TLS-сертификат и параметры соединения: издатель, срок, SAN, версия TLS, шифр.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Домен, IP или URL"},
            "port": {"type": "integer", "description": "Порт TLS (по умолчанию 443)"},
            "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 10)"},
        },
        "required": ["target"],
    },
)
def tls_info(args: dict, ctx) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("Не указана цель.")
    host = normalize_target(target)
    reason = _scope_reason(ctx, host)
    port = max(1, min(65535, int(args.get("port") or 443)))
    timeout = max(2, min(30, int(args.get("timeout") or 10)))
    info = _tls_info(host, port, timeout)
    lines = [
        f"TLS: {host}:{port}",
        f"Скоуп: {reason}",
        f"Проверен: {'да' if info.get('verified') else 'нет'}",
        f"Версия: {info.get('version') or '—'}",
        f"Шифр: {info.get('cipher') or '—'}",
        f"Издатель: {info.get('issuer') or '—'}",
        f"Субъект: {info.get('subject') or '—'}",
        f"Действует с: {info.get('not_before') or '—'}",
        f"Действует до: {info.get('not_after') or '—'}",
        f"SAN: {', '.join(info.get('san', [])) or '—'}",
    ]
    if info.get("error"):
        lines.append(f"Ошибка: {info['error']}")
    if info.get("error_retry"):
        lines.append(f"Повтор без проверки: {info['error_retry']}")
    return "\n".join(lines)


def _same_site(first: str, second: str) -> bool:
    host_a = (urllib.parse.urlsplit(first).hostname or "").lower()
    host_b = (urllib.parse.urlsplit(second).hostname or "").lower()
    return host_a.removeprefix("www.") == host_b.removeprefix("www.")


def _extract_links(base_url: str, html: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"""(?:href|src)\s*=\s*["']([^"'#]+)""", html, re.I):
        raw = match.group(1).strip()
        if raw.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "blob:")):
            continue
        absolute = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        clean = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
        )
        if clean not in links:
            links.append(clean)
    return links


def _extract_forms(base_url: str, html: str) -> list[tuple[str, str]]:
    forms: list[tuple[str, str]] = []
    for match in re.finditer(r"<form\b[^>]*>", html, re.I):
        tag = match.group(0)
        action = re.search(r"""action\s*=\s*["']([^"']*)["']""", tag, re.I)
        method = re.search(r"""method\s*=\s*["']([^"']*)["']""", tag, re.I)
        target = urllib.parse.urljoin(base_url, action.group(1) if action else base_url)
        forms.append((target, (method.group(1) if method else "get").upper()))
    return forms


@tool(
    "crawl_site",
    "Обойти страницы одного сайта (до 30), собрать ссылки, формы, email и внешние домены, "
    "сохранить Markdown-отчёт. Публичные цели — только из авторизованного скоупа.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Стартовый URL или домен"},
            "max_pages": {"type": "integer", "description": "Сколько страниц обойти (по умолчанию 10, максимум 30)"},
            "timeout": {"type": "integer", "description": "Таймаут запроса, сек (по умолчанию 12)"},
        },
        "required": ["url"],
    },
)
def crawl_site(args: dict, ctx) -> str:
    start_url = _normalize_url(args.get("url"))
    host = normalize_target(start_url)
    reason = _scope_reason(ctx, host)
    max_pages = max(1, min(30, int(args.get("max_pages") or 10)))
    timeout = max(5, min(30, int(args.get("timeout") or 12)))
    started = datetime.now()

    queue: deque[str] = deque([start_url])
    seen: set[str] = set()
    rows: list[tuple[str, str, str, str]] = []
    forms: list[tuple[str, str, str]] = []
    emails: list[str] = []
    external: list[str] = []

    while queue and len(seen) < max_pages:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        response, body, error = _request(current, timeout=timeout, max_body=MAX_BODY)
        if response is None:
            rows.append((current, "ERR", f"ошибка: {error}", ""))
            continue

        final_url = response.geturl() if hasattr(response, "geturl") else current
        headers = response.headers
        content_type = (headers.get("Content-Type", "") if headers else "").split(";", 1)[0]
        html = _decode_body(body, headers)
        title = _title(html)
        rows.append((current, str(getattr(response, "status", "?")), title or "—", content_type or "—"))

        if content_type and "html" not in content_type.lower():
            continue

        for link in _extract_links(final_url, html):
            path = urllib.parse.urlsplit(link).path.lower()
            if any(token in path for token in SKIP_TOKENS):
                continue
            if _same_site(final_url, link):
                suffix = Path(path).suffix.lower()
                if suffix in SKIP_EXTENSIONS:
                    continue
                if link not in seen and link not in queue and len(queue) + len(seen) < max_pages * 4:
                    queue.append(link)
            elif link not in external:
                external.append(link)

        for target, method in _extract_forms(final_url, html):
            item = (final_url, method, target)
            if item not in forms:
                forms.append(item)

        for email in EMAIL_RE.findall(html):
            if email not in emails and len(emails) < 100:
                emails.append(email)

    lines = [
        f"# Обход сайта: {host}",
        "",
        f"- Дата: {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Старт: {start_url}",
        f"- Скоуп: {reason}",
        f"- Проверено страниц: {len(rows)}",
        "",
        "## Страницы",
        "| URL | Статус | Заголовок | Content-Type |",
        "|---|---|---|---|",
    ]
    lines.extend(f"| {url} | {status} | {title} | {ctype} |" for url, status, title, ctype in rows)

    lines.extend(["", "## Формы"])
    if forms:
        lines.extend(["| Страница | Метод | Action |", "|---|---|---|"])
        lines.extend(f"| {page} | {method} | {target} |" for page, method, target in forms)
    else:
        lines.append("Форм не найдено.")

    lines.extend(["", "## Email"])
    lines.extend([f"- {email}" for email in emails] or ["- не найдено"])

    lines.extend(["", "## Внешние ссылки"])
    lines.extend([f"- {url}" for url in external[:100]] or ["- не найдено"])

    reports = Path(ctx.workspace) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    safe_host = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host)[:80] or "target"
    report_path = reports / f"crawl_{safe_host}_{started.strftime('%Y%m%d-%H%M%S')}.md"
    write_text(report_path, "\n".join(lines) + "\n")

    ctx.audit.write(
        "crawl_site",
        url=start_url,
        pages=len(rows),
        forms=len(forms),
        emails=len(emails),
        report=str(report_path),
    )
    return "\n".join(
        [
            f"Обход {host} завершён.",
            f"Скоуп: {reason}",
            f"Страниц: {len(rows)}, форм: {len(forms)}, email: {len(emails)}, внешних ссылок: {len(external)}",
            f"Отчёт: {report_path}",
        ]
    )