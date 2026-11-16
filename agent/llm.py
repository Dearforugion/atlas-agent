from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

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

_HINT = (
    "Проверь, что сервер модели запущен и base_url указан верно. "
    "Быстрая диагностика: run.cmd doctor"
)


class LLM:
    """Клиент любого OpenAI-совместимого API (Ollama, LM Studio, OpenAI, OpenRouter...)."""

    def __init__(self, config: LLMSettings) -> None:
        self.config = config

    @property
    def url(self) -> str:
        return self.config.base_url.rstrip("/")

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: int | None = None) -> str:
        url = self.url + path
        data = None
        headers = {"Content-Type": "application/json", "User-Agent": "ATLAS-local-agent/1.0"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
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

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        raw = self._request("POST", "/chat/completions", payload)
        try:
            data = json.loads(raw)
            message = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ модели: {raw[:1000]}") from exc

        if not isinstance(message, dict):
            raise LLMError(f"Неожиданный формат сообщения: {message!r}")
        message.setdefault("role", "assistant")
        content = message.get("content")
        if content is None:
            message["content"] = ""
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and str(block.get("text", "")).strip():
                    parts.append(str(block["text"]))
            message["content"] = "\n".join(parts)
        # Убираем блоки рассуждения <think>...</think> и отдельное поле reasoning,
        # чтобы они не попали в ответ пользователю и в разбор текстовых tool_call.
        message["content"] = _strip_thinking(str(message.get("content") or ""))
        message.pop("reasoning", None)
        message.pop("reasoning_content", None)
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