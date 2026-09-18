"""Память бота: профиль клиента, резюме прошлых разговоров, история эпизода.

Переписку помнит бот, а не модель. Модель каждый раз получает короткий
контекст: профиль, резюме и последние сообщения эпизода. Вся история не
отправляется никогда: она дорожает с каждым сообщением, и чем больше старого
текста в запросе, тем хуже модель понимает, что сейчас важно.

Резюме считается не после каждого ответа, а когда после прошлого резюме
накопилось settings.summary_every_messages сообщений, и в начале нового
эпизода, если остался необобщённый хвост.

Профиль и резюме это данные о клиенте. В логи они не попадают: там имена,
цели и часовые пояса.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .db import DB_ERRORS
from .llm import LLMError, OpenAICompatClient
from .prompts import (
    PROFILE_FIELDS, SUMMARY_SCHEMA_NAME, SUMMARY_JSON_SCHEMA, ClientSummary, build_summary_messages,
)
from .repo import Repo

log = logging.getLogger(__name__)

# Сколько реплик максимум уходит в один пересчёт резюме.
SUMMARY_INPUT_LIMIT = 60
# Длина одного значения профиля: имя и цель, а не сочинение.
PROFILE_VALUE_MAX = 200


@dataclass(frozen=True)
class Memory:
    profile: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)


def clean_profile_patch(raw: dict[str, Any], known: dict[str, Any]) -> dict[str, Any]:
    """Оставляет только поля из белого списка, непустые и новые.

    Модель возвращает null почти во всех полях: это нормально, так она говорит
    «клиент об этом не сказал».
    """
    patch: dict[str, Any] = {}
    for key in PROFILE_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()[:PROFILE_VALUE_MAX]
            if not value:
                continue
        if known.get(key) == value:
            continue
        patch[key] = value
    return patch


class MemoryService:
    def __init__(self, settings: Settings, repo: Repo, llm: OpenAICompatClient) -> None:
        self._s = settings
        self._repo = repo
        self._llm = llm

    async def load(
        self, client_id: int, conversation_id: int, *, exclude_message_id: int | None = None
    ) -> Memory:
        profile, summary, _summary_message_id = await self._repo.client_memory(client_id)
        history = await self._repo.recent_messages(
            conversation_id, self._s.history_messages, exclude_message_id
        )
        return Memory(profile=profile, summary=summary, history=history)

    def clean_updates(self, raw: dict[str, Any], known: dict[str, Any]) -> dict[str, Any]:
        """Что из присланного моделью попадёт в профиль."""
        return clean_profile_patch(raw, known)

    async def update_summary_if_needed(
        self, client_id: int, *, force: bool = False, before_message_id: int | None = None
    ) -> bool:
        """force = новый эпизод: обобщаем любой хвост, даже короткий.

        У первого в жизни клиента эпизода хвоста нет, и вызова модели не будет.
        """
        try:
            _profile, old_summary, summary_message_id = await self._repo.client_memory(client_id)
            pending, last_id = await self._repo.count_since(
                client_id, summary_message_id, before_message_id
            )
            if pending == 0 or (not force and pending < self._s.summary_every_messages):
                return False
            history = await self._repo.messages_since(
                client_id, summary_message_id, SUMMARY_INPUT_LIMIT, before_message_id
            )
            if not history:
                return False
        except DB_ERRORS as exc:
            log.error("резюме не собрано", extra={"client_id": client_id, "error": type(exc).__name__})
            return False

        messages = build_summary_messages(self._s, old_summary, history)
        try:
            result = await self._llm.chat(
                model=self._s.llm_model,
                messages=messages,
                json_schema=SUMMARY_JSON_SCHEMA,
                schema_name=SUMMARY_SCHEMA_NAME,
                max_tokens=self._s.llm_max_tokens,
                reasoning_effort=self._s.llm_reasoning_effort,
                provider=self._s.llm_provider,
            )
        except LLMError as exc:
            await self._log_call(client_id, exc.latency_ms, error=exc.kind)
            return False

        try:
            summary = ClientSummary.model_validate_json(result.content).summary.strip()
        except ValidationError:
            await self._log_call(client_id, result.latency_ms, error="invalid_json")
            return False
        await self._log_call(
            client_id, result.latency_ms,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        )
        if not summary:
            return False

        try:
            await self._repo.save_summary(client_id, summary[: self._s.summary_max_chars], last_id)
        except DB_ERRORS as exc:
            log.error("резюме не сохранено", extra={"client_id": client_id, "error": type(exc).__name__})
            return False
        log.info(
            "резюме обновлено",
            extra={"client_id": client_id, "messages": pending, "summary_len": len(summary), "forced": force},
        )
        return True

    async def _log_call(
        self,
        client_id: int,
        latency_ms: int,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        if error:
            log.warning("резюме не получено", extra={"client_id": client_id, "error": error})
        try:
            await self._repo.add_llm_call(
                purpose="summary", model=self._s.llm_model, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, error=error,
            )
        except DB_ERRORS as exc:
            log.error("не записан вызов модели", extra={"purpose": "summary", "error": type(exc).__name__})
