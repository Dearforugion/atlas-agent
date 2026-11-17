from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

from .errors import LLMError
from .settings import LLMSettings

# Модели с рассуждением (qwen3 и др.) оборачивают «мысли» в <think>...</think>.
# Эти блоки помогают модели думать, но не должны попадать ни в финальный ответ
# пользователю, ни в парсер текстовых вызовов инструментов.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    if not text or "<think" not in text.lower():
        return text
    cleaned = _THINK_RE.sub("", text)
    # Незакрытый <think> (обрыв по лимиту токенов) — отбрасываем хвост.
    cleaned = _OPEN_THINK_RE.sub("", cleaned)
    return cleaned.strip()


class _ThinkFilter:
    """Инкрементально скрывает <think>...</think> из потока токенов.

    Тег может прийти разрезанным на несколько чанков, поэтому хвост,
    похожий на начало тега, придерживается до следующего вызова.
    """

    OPEN = "<think"
    CLOSE = "</think"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            low = self._buffer.lower()
            if self._inside:
                end = low.find(self.CLOSE)
                if end < 0:
                    keep = self._partial_tail(low, self.CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
                    break
                close = self._buffer.find(">", end)
                if close < 0:
                    self._buffer = self._buffer[end:]
                    break
                self._buffer = self._buffer[close + 1:]
                self._inside = False
                continue
            start = low.find(self.OPEN)
            if start < 0:
                keep = self._partial_tail(low, self.OPEN)
                cut = len(self._buffer) - keep
                out.append(self._buffer[:cut])
                self._buffer = self._buffer[cut:]
                break
            out.append(self._buffer[:start])
            open_end = self._buffer.find(">", start)
            if open_end < 0:
                self._buffer = self._buffer[start:]
                break
            self._buffer = self._buffer[open_end + 1:]
            self._inside = True
        return "".join(out)

    @staticmethod
    def _partial_tail(low: str, token: str) -> int:
        max_len = min(len(low), len(token) - 1)
        for size in range(max_len, 0, -1):
            if token.startswith(low[-size:]):
                return size
        return 0

    def flush(self) -> str:
        if self._inside:
            self._buffer = ""
            return ""
        text, self._buffer = self._buffer, ""
        return text


_HINT = (
    "Проверь, что сервер модели запущен и base_url указан верно. "
    "Быстрая диагностика: run.cmd doctor"
)


def _coerce_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and str(block.get("text", "")).strip():
                parts.append(str(block["text"]))
        return "\n".join(parts)
    return str(content)


def _normalize_message(message: dict) -> dict:
    if not isinstance(message, dict):
        raise LLMError(f"Неожиданный формат сообщения: {message!r}")
    message.setdefault("role", "assistant")
    message["content"] = _strip_thinking(_coerce_content(message.get("content")))
    # Отдельные поля рассуждения не должны попадать ни в чат, ни в историю.
    message.pop("reasoning", None)
    message.pop("reasoning_content", None)
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        normalized: list[dict] = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            elif not isinstance(arguments, str):
                arguments = "{}"
            normalized.append(
                {
                    "id": str(call.get("id") or f"call_{index}"),
                    "type": "function",
                    "function": {"name": name, "arguments": arguments or "{}"},
                }
            )
        if normalized:
            message["tool_calls"] = normalized
        else:
            message.pop("tool_calls", None)
    elif calls is not None:
        message.pop("tool_calls", None)
    return message


def _merge_tool_calls(acc: dict[int, dict], chunks) -> None:
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        raw_index = chunk.get("index")
        index: int | None
        try:
            index = int(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            index = None
        chunk_id = str(chunk.get("id") or "")
        if index is None:
            if chunk_id:
                index = next(
                    (i for i, slot in acc.items() if slot.get("id") == chunk_id),
                    None,
                )
            if index is None:
                index = max(acc) + 1 if acc else 0
        slot = acc.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if chunk_id:
            slot["id"] = chunk_id
        if chunk.get("type"):
            slot["type"] = str(chunk["type"])
        function = chunk.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if name:
            name = str(name)
            current_name = slot["function"]["name"]
            if not current_name:
                slot["function"]["name"] = name
            elif current_name != name and not current_name.endswith(name):
                slot["function"]["name"] = current_name + name
        arguments = function.get("arguments")
        if arguments is not None:
            slot["function"]["arguments"] = _merge_arguments(
                slot["function"]["arguments"], arguments
            )


def _merge_arguments(current, new):
    if isinstance(new, dict):
        return json.dumps(new, ensure_ascii=False)
    text = str(new)
    if not current:
        return text
    if not isinstance(current, str):
        current = str(current)
    if text == current:
        return current
    # Некоторые провайдеры присылают накопленную строку целиком.
    if text.startswith(current):
        return text
    return current + text


def _finalize_tool_calls(acc: dict[int, dict]) -> list[dict]:
    calls: list[dict] = []
    for index in sorted(acc):
        slot = acc[index]
        name = str(slot["function"]["name"]).strip()
        if not name:
            continue
        arguments = slot["function"]["arguments"]
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not arguments.strip():
            arguments = "{}"
        calls.append(
            {
                "id": slot.get("id") or f"call_{index}",
                "type": slot.get("type") or "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return calls


class LLM:
    """Клиент любого OpenAI-совместимого API (Ollama, LM Studio, OpenAI, OpenRouter...)."""

    def __init__(self, config: LLMSettings) -> None:
        self.config = config

    @property
    def url(self) -> str:
        return self.config.base_url.rstrip("/")

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "ATLAS-local-agent/1.0",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: int | None = None) -> str:
        url = self.url + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise LLMError(f"HTTP {exc.code} от {url}: {detail}\n{_HINT}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Не удалось подключиться к {url}: {exc.reason}\n{_HINT}") from exc
        except TimeoutError as exc:
            raise LLMError(f"Таймаут при обращении к {url}.\n{_HINT}") from exc

    def _payload(self, messages: list[dict], tools: list[dict] | None, stream: bool) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _parse_completion(self, raw: str) -> dict:
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice.get("message") or choice.get("delta") or {}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ модели: {raw[:1000]}") from exc
        return _normalize_message(message)

    def _chat_once(self, messages: list[dict], tools: list[dict] | None) -> dict:
        payload = self._payload(messages, tools, stream=False)
        raw = self._request("POST", "/chat/completions", payload)
        return self._parse_completion(raw)

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> dict:
        if on_token is not None and self.config.stream:
            emitted = False

            def wrapped(text: str) -> None:
                nonlocal emitted
                if text:
                    emitted = True
                    on_token(text)

            try:
                return self._chat_stream(messages, tools, wrapped)
            except LLMError:
                # Если сервер не умеет SSE и ещё ничего не показано —
                # повторяем обычным запросом. После первых токенов
                # повторять нельзя: ответ уже частично в чате.
                if emitted:
                    raise
                return self._chat_once(messages, tools)
        return self._chat_once(messages, tools)

    def _chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        on_token: Callable[[str], None],
    ) -> dict:
        url = self.url + "/chat/completions"
        payload = self._payload(messages, tools, stream=True)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method="POST"
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise LLMError(f"HTTP {exc.code} от {url}: {detail}\n{_HINT}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Не удалось подключиться к {url}: {exc.reason}\n{_HINT}") from exc
        except TimeoutError as exc:
            raise LLMError(f"Таймаут при обращении к {url}.\n{_HINT}") from exc

        think = _ThinkFilter()
        content_parts: list[str] = []
        tool_slots: dict[int, dict] = {}

        try:
            with response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    # Сервер проигнорировал stream=true и вернул обычный JSON.
                    raw = response.read().decode("utf-8", "replace")
                    message = self._parse_completion(raw)
                    text = str(message.get("content") or "")
                    if text:
                        on_token(text)
                    return message

                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        event = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("error"):
                        raise LLMError(f"Ошибка потока модели: {event['error']}")
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    delta = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(delta, dict):
                        continue
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        content_parts.append(text)
                        visible = think.feed(text)
                        if visible:
                            on_token(visible)
                    calls = delta.get("tool_calls")
                    if calls:
                        _merge_tool_calls(tool_slots, calls)
        except TimeoutError as exc:
            raise LLMError(f"Таймаут чтения потока от {url}.\n{_HINT}") from exc
        except OSError as exc:
            raise LLMError(f"Обрыв соединения с {url}: {exc}\n{_HINT}") from exc

        tail = think.flush()
        if tail:
            on_token(tail)

        message: dict = {"role": "assistant", "content": _strip_thinking("".join(content_parts))}
        calls = _finalize_tool_calls(tool_slots)
        if calls:
            message["tool_calls"] = calls
        return message

    def list_models(self) -> list[str]:
        raw = self._request("GET", "/models", timeout=15)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Сервер вернул не JSON: {raw[:300]}") from exc
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        result = []
        for item in models:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    result.append(str(model_id))
        return sorted(result)
