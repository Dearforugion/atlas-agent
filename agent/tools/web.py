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
    search_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)

    final_url, content_type, data = _fetch(search_url, allow_private=ctx.settings.allow_private_web)
    markup = _decode_body(data, content_type)
    anchor_re = re.compile(
        r"<a\b([^>]*\bclass=\"[^\"]*result__a[^\"]*\"[^>]*)>(.*?)</a>",
        re.S | re.I,
    )
    results: list[str] = []
    seen: set[str] = set()
    for match in anchor_re.finditer(markup):
        attrs, inner = match.group(1), match.group(2)
        href_match = re.search(r"href=\"([^\"]+)\"", attrs, re.I)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1))
        href = _unwrap_ddg(href)
        title = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        results.append(f"{len(results) + 1}. {title}\n   {href}")
        if len(results) >= limit:
            break

    ctx.audit.write("web_search", query=query, results=len(results))
    if not results:
        return (
            "Поисковик не вернул результатов (возможно, ограничил запрос). "
            "Попробуй сформулировать иначе или используй web_fetch по известной ссылке."
        )
    return f"Результаты по запросу «{query}»:\n\n" + "\n".join(results)


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