from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ..errors import ToolError
from ..safety import is_private_host
from . import tool

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_DOWNLOAD = 20_000_000


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "pre", "blockquote", "table",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return parser.text()


def _decode_body(data: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "cp1251"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _check_url(url: str, allow_private: bool = False) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Разрешены только http/https-ссылки.")
    if not parsed.hostname:
        raise ToolError("В ссылке нет имени хоста.")
    if not allow_private and is_private_host(parsed.hostname):
        raise ToolError("Обращение к локальной сети и localhost запрещено.")
    return parsed


def _fetch(url: str, timeout: int = 30, allow_private: bool = False) -> tuple[str, str, bytes]:
    _check_url(url, allow_private)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            _check_url(final_url, allow_private)  # защита от редиректа в локальную сеть
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_DOWNLOAD + 1)
            return final_url, content_type, data
    except urllib.error.HTTPError as exc:
        raise ToolError(f"Сайт вернул HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Не удалось загрузить {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ToolError(f"Таймаут при загрузке {url}.") from exc


@tool(
    "web_fetch",
    "Загрузить веб-страницу и вернуть её текст (HTML очищается от разметки).",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полная ссылка http(s)://..."},
            "max_chars": {"type": "integer", "description": "Максимум символов текста (по умолчанию 10000)"},
        },
        "required": ["url"],
    },
)
def web_fetch(args: dict, ctx) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolError("Пустая ссылка.")
    max_chars = max(500, min(30000, int(args.get("max_chars") or 10000)))

    final_url, content_type, data = _fetch(url, allow_private=ctx.settings.allow_private_web)
    if len(data) > MAX_DOWNLOAD:
        data = data[:MAX_DOWNLOAD]
        truncated_note = f"\n... [страница обрезана на {MAX_DOWNLOAD // (1024 * 1024)} МБ]"
    else:
        truncated_note = ""

    text = _decode_body(data, content_type)
    lowered = content_type.lower()
    if "html" in lowered or text.lstrip().lower().startswith(("<!doctype", "<html")):
        text = _html_to_text(text)
        kind = "html->text"
    else:
        kind = lowered.split(";")[0] or "text"

    text = text[:max_chars] + truncated_note
    ctx.audit.write("web_fetch", url=final_url, bytes=len(data))
    return f"URL: {final_url}\nТип: {kind}\n\n{text or '(пусто)'}"


@tool(
    "web_search",
    "Найти информацию в интернете через DuckDuckGo и вернуть ссылки с заголовками.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "max_results": {"type": "integer", "description": "Сколько результатов вернуть (до 10)"},
        },
        "required": ["query"],
    },
)
def web_search(args: dict, ctx) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Пустой поисковый запрос.")
    limit = max(1, min(10, int(args.get("max_results") or 6)))
    allow_private = ctx.settings.allow_private_web

    engines = (
        ("duckduckgo", _search_ddg_html, "https://html.duckduckgo.com/html/?q="),
        ("duckduckgo-lite", _search_ddg_lite, "https://lite.duckduckgo.com/lite/?q="),
    )
    problems: list[str] = []
    for engine, parser, base in engines:
        url = base + urllib.parse.quote_plus(query)
        try:
            _, content_type, data = _fetch(url, allow_private=allow_private)
            markup = _decode_body(data, content_type)
            results = parser(markup, limit)
        except ToolError as exc:
            problems.append(f"{engine}: {exc}")
            continue
        if results:
            ctx.audit.write("web_search", query=query, results=len(results), engine=engine)
            return f"Результаты по запросу «{query}»:\n\n" + "\n".join(results)
        problems.append(f"{engine}: пустой результат")

    ctx.audit.write("web_search", query=query, results=0, errors=problems)
    return (
        "Поисковик не вернул результатов (возможно, ограничил запрос). "
        "Попробуй сформулировать иначе, открой известную ссылку через web_fetch "
        "или запроси страницу через run_shell. Детали: " + "; ".join(problems)
    )


def _search_ddg_html(markup: str, limit: int) -> list[str]:
    return _extract_anchors(markup, "result__a", limit)


def _search_ddg_lite(markup: str, limit: int) -> list[str]:
    return _extract_anchors(markup, "result-link", limit)


_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)


def _extract_links(markup: str, class_token: str, limit: int) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for attrs, inner in _ANCHOR_RE.findall(markup):
        class_match = re.search(r"\bclass\s*=\s*['\"]([^'\"]*)['\"]", attrs, re.I)
        if not class_match or class_token not in class_match.group(1):
            continue
        href_match = re.search(r"\bhref\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.I)
        if not href_match:
            continue
        href = _unwrap_ddg(html.unescape(href_match.group(1)))
        title = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not href.startswith(("http://", "https://")) or not title or href in seen:
            continue
        seen.add(href)
        results.append((title, href))
        if len(results) >= limit:
            break
    return results


def _extract_anchors(markup: str, class_token: str, limit: int) -> list[str]:
    return [
        f"{index}. {title}\n   {url}"
        for index, (title, url) in enumerate(
            _extract_links(markup, class_token, limit), 1
        )
    ]


def _search_links(
    query: str, limit: int, allow_private: bool
) -> tuple[list[tuple[str, str]], list[str]]:
    engines = (
        (_search_ddg_html_links, "https://html.duckduckgo.com/html/?q="),
        (_search_ddg_lite_links, "https://lite.duckduckgo.com/lite/?q="),
    )
    problems: list[str] = []
    for parser, base in engines:
        url = base + urllib.parse.quote_plus(query)
        try:
            _, content_type, data = _fetch(url, allow_private=allow_private)
            markup = _decode_body(data, content_type)
            links = parser(markup, limit)
        except ToolError as exc:
            problems.append(str(exc))
            continue
        if links:
            return links, problems
        problems.append("пустой результат")
    return [], problems


def _search_ddg_html_links(markup: str, limit: int) -> list[tuple[str, str]]:
    return _extract_links(markup, "result__a", limit)


def _search_ddg_lite_links(markup: str, limit: int) -> list[tuple[str, str]]:
    return _extract_links(markup, "result-link", limit)


def _error_excerpt(text: str, needles: list[str], limit: int) -> str:
    lowered = text.lower()
    for needle in needles:
        needle = needle.strip().lower()
        if len(needle) < 4:
            continue
        position = lowered.find(needle)
        if position < 0:
            continue
        start = max(0, position - 300)
        excerpt = text[start:start + limit].strip()
        return ("…" if start else "") + excerpt
    return text[:limit].strip()


@tool(
    "research_error",
    "Исследовать ошибку в интернете: найти страницы с решением, открыть лучшие "
    "и вернуть выдержки. Используй, когда инструмент или команда падает, а "
    "причина неочевидна.",
    {
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "Текст ошибки или ключевая строка вывода"},
            "context": {"type": "string", "description": "Что запускали: инструмент, команда, ОС"},
            "max_pages": {
                "type": "integer",
                "description": "Сколько страниц открыть для выдержек (1-3, по умолчанию 2)",
            },
        },
        "required": ["error"],
    },
)
def research_error(args: dict, ctx) -> str:
    error = re.sub(r"\s+", " ", str(args.get("error") or "")).strip()
    if not error:
        raise ToolError("Пустой текст ошибки.")
    context = re.sub(r"\s+", " ", str(args.get("context") or "")).strip()
    pages = max(1, min(3, int(args.get("max_pages") or 2)))
    allow_private = ctx.settings.allow_private_web

    query = error[:220]
    if context:
        query = f"{query} {context[:120]}"
    query = f"{query} fix solution"

    links, problems = _search_links(query, 6, allow_private)
    if not links:
        # Второй заход с более коротким запросом — поисковики лучше
        # реагируют на пару ключевых слов, чем на длинную строку.
        short = " ".join(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{4,}", error)[:6])
        if short and short.lower() != query.lower():
            links, problems2 = _search_links(short, 6, allow_private)
            problems += problems2
    if not links:
        ctx.audit.write("research_error", error=error[:300], results=0)
        return (
            "Поиск не дал результатов. Попробуй сформулировать ошибку короче, "
            "проверить официальную документацию инструмента или выполнить "
            "команду с флагом диагностики. Детали: " + "; ".join(problems)
        )

    needles = [error[:80]] + [
        token for token in re.findall(r"[A-Za-zА-Яа-я0-9_.-]{5,}", error)[:8]
    ]
    parts = [f"Запрос: {query}", "Источники:"]
    for index, (title, url) in enumerate(links, 1):
        parts.append(f"{index}. {title}\n   {url}")

    opened = 0
    for title, url in links:
        if opened >= pages:
            break
        try:
            final_url, content_type, data = _fetch(url, allow_private=allow_private)
        except ToolError as exc:
            parts.append(f"\n[не открылось] {url}: {exc}")
            continue
        text = _decode_body(data, content_type)
        if "html" in content_type.lower() or text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = _html_to_text(text)
        excerpt = _error_excerpt(text, needles, 1500)
        if not excerpt:
            continue
        opened += 1
        parts.append(f"\nФрагмент: {final_url}\n{excerpt}")

    ctx.audit.write("research_error", error=error[:300], results=len(links), opened=opened)
    if not opened:
        parts.append(
            "\nНе удалось открыть страницы — используй ссылки выше через web_fetch."
        )
    return "\n".join(parts)


def _unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href