"""Клиент OpenAI-совместимого API: чат со структурированным ответом и эмбеддинги.

Провайдер сейчас OpenRouter, но в клиенте о нём знают только два необязательных
поля запроса: reasoning и provider. Без них запрос годится для любого
OpenAI-совместимого API.

Текст запросов и ответов в логи не попадает. Ошибка несёт только вид сбоя
(timeout, http_429, invalid_json), без тела ответа провайдера: в нём бывает
эхо запроса, то есть текст клиента.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class LLMError(Exception):
    """Вызов не удался. kind короткий и безопасен для логов и базы."""

    def __init__(self, kind: str, latency_ms: int) -> None:
        super().__init__(kind)
        self.kind = kind
        self.latency_ms = latency_ms


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    prompt_tokens: int | None
    latency_ms: int


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, timeout_sec: float) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            response = await self._client.post(f"{self._base}{path}", json=body)
        except httpx.TimeoutException:
            raise LLMError("timeout", elapsed()) from None
        except httpx.HTTPError:
            raise LLMError("network", elapsed()) from None
        if response.status_code != 200:
            raise LLMError(f"http_{response.status_code}", elapsed())
        try:
            payload = response.json()
        except ValueError:
            raise LLMError("invalid_response", elapsed()) from None
        if not isinstance(payload, dict) or payload.get("error"):
            # OpenRouter может вернуть ошибку провайдера и с кодом 200.
            raise LLMError("provider_error", elapsed())
        return payload, elapsed()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str | None = None,
        provider: dict[str, Any] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            },
        }
        if reasoning_effort is not None:
            body["reasoning"] = {"effort": reasoning_effort}
        if provider:
            body["provider"] = provider

        payload, latency = await self._post("/chat/completions", body)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("invalid_response", latency) from None
        if choice.get("finish_reason") == "length":
            # JSON обрезан на середине, разбирать его бессмысленно.
            raise LLMError("truncated", latency)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty", latency)
        usage = payload.get("usage") or {}
        return ChatResult(
            content=content,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=latency,
        )

    async def embed(
        self,
        *,
        model: str,
        texts: list[str],
        dim: int,
        provider: dict[str, Any] | None = None,
    ) -> EmbedResult:
        body: dict[str, Any] = {"model": model, "input": texts}
        if provider:
            body["provider"] = provider

        payload, latency = await self._post("/embeddings", body)
        try:
            items = sorted(payload["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError):
            raise LLMError("invalid_response", latency) from None
        if len(vectors) != len(texts):
            raise LLMError("invalid_response", latency)
        if any(len(v) != dim for v in vectors):
            # Модель отдаёт вектор другой размерности, чем колонка в базе.
            raise LLMError("dimension_mismatch", latency)
        usage = payload.get("usage") or {}
        return EmbedResult(vectors=vectors, prompt_tokens=usage.get("prompt_tokens"), latency_ms=latency)
