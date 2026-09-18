"""Конвейер ответа: вопрос -> эмбеддинг -> поиск -> модель -> проверенный ответ.

Один вызов модели на сообщение, без агентного цикла. Код не верит модели на
слово в трёх местах:
- метки источников сверяются с тем, что модель реально видела;
- ответ по делу без единого источника превращается в передачу руководителю;
- битый JSON, таймаут и ошибка провайдера дают нейтральный текст, а не падение.

Ошибки базы отсюда не ловятся: их ловит обработчик и отвечает клиенту
извинением, как на любом другом шаге.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from . import texts
from .config import Settings
from .db import DB_ERRORS
from .knowledge import ALWAYS_IN_CONTEXT
from .llm import LLMError, OpenAICompatClient
from .memory import Memory
from .prompts import (
    REPLY_JSON_SCHEMA, REWRITE_JSON_SCHEMA, REWRITE_SCHEMA_NAME, SCHEMA_NAME,
    ModelReply, RewrittenQuery, build_rewrite_messages, build_system_prompt,
)
from .repo import FoundChunk, Repo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    text: str
    needs_human: bool
    handoff_reason: str | None
    meta: dict[str, Any] = field(default_factory=dict)
    # Новые факты о клиенте: обработчик кладёт их в профиль вместе с ответом.
    profile_updates: dict[str, Any] = field(default_factory=dict)
    # Заявка, если клиент явно попросил записать его. Иначе None.
    lead: dict[str, Any] | None = None


class AnswerService:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        llm: OpenAICompatClient,
        embedder: OpenAICompatClient,
    ) -> None:
        self._s = settings
        self._repo = repo
        self._llm = llm
        self._embedder = embedder

    async def answer(
        self,
        question: str,
        *,
        conversation_id: int | None,
        message_id: int | None,
        memory: Memory | None = None,
    ) -> Answer:
        s = self._s
        memory = memory or Memory()
        started = time.monotonic()
        ids = {"conversation_id": conversation_id, "message_id": message_id}

        search_query = await self._search_query(question, memory, ids)
        try:
            embedded = await self._embedder.embed(
                model=s.embedding_model, texts=[search_query], dim=s.embedding_dim,
                provider=s.embedding_provider,
            )
        except LLMError as exc:
            await self._log_call("embed", s.embedding_model, exc.latency_ms, ids, error=exc.kind)
            return self._fallback("llm_error", {"stage": "embed", "error": exc.kind})
        await self._log_call(
            "embed", s.embedding_model, embedded.latency_ms, ids, prompt_tokens=embedded.prompt_tokens
        )

        candidates = await self._repo.search_chunks(embedded.vectors[0], s.search_top_k, ALWAYS_IN_CONTEXT)
        found = [c for c in candidates if c.score >= s.search_min_score]
        limitations = await self._repo.document_chunks(ALWAYS_IN_CONTEXT)
        system_prompt, labels = build_system_prompt(
            s, limitations, found, profile=memory.profile, summary=memory.summary
        )
        history_turns = [
            {"role": "user" if role == "client" else "assistant", "content": text}
            for role, text in memory.history
        ]

        try:
            chat = await self._llm.chat(
                model=s.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *history_turns,
                    {"role": "user", "content": question},
                ],
                json_schema=REPLY_JSON_SCHEMA,
                schema_name=SCHEMA_NAME,
                max_tokens=s.llm_max_tokens,
                reasoning_effort=s.llm_reasoning_effort,
                provider=s.llm_provider,
            )
        except LLMError as exc:
            await self._log_call("answer", s.llm_model, exc.latency_ms, ids, error=exc.kind)
            return self._fallback("llm_error", {"stage": "answer", "error": exc.kind})

        try:
            parsed = ModelReply.model_validate_json(chat.content)
        except ValidationError:
            parsed = None
        await self._log_call(
            "answer", s.llm_model, chat.latency_ms, ids,
            prompt_tokens=chat.prompt_tokens, completion_tokens=chat.completion_tokens,
            error=None if parsed else "invalid_json",
        )
        if parsed is None:
            return self._fallback("invalid_json", {"stage": "answer", "error": "invalid_json"})

        known = [label for label in dict.fromkeys(parsed.sources) if label in labels]
        needs_human = parsed.needs_human
        reason: str | None = parsed.handoff_reason if needs_human else None
        if needs_human and reason is None:
            reason = "unspecified"
        if not needs_human and not parsed.is_smalltalk and not known:
            # Ответ по делу, но опереться не на что: это и есть выдумка.
            needs_human, reason = True, "no_sources"
        if not needs_human and not parsed.reply.strip():
            needs_human, reason = True, "empty_reply"

        meta = {
            "model": s.llm_model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "history_messages": len(history_turns),
            "search_query": search_query if search_query != question else None,
            "prompt_tokens": chat.prompt_tokens,
            "completion_tokens": chat.completion_tokens,
            "retrieved": [{"chunk_id": c.id, "path": c.path, "score": round(c.score, 3)} for c in found],
            "sources": [self._source(labels[label]) for label in known],
            "unknown_sources": len(set(parsed.sources) - set(known)),
            "is_smalltalk": parsed.is_smalltalk,
            "needs_human": needs_human,
            "handoff_reason": reason,
        }
        # Файлы и разделы базы знаний это не текст клиента, их в лог можно:
        # по ним видно, что нашёл поиск и на что опёрлась модель.
        log.info(
            "ответ модели",
            extra={
                **ids,
                "latency_ms": meta["latency_ms"],
                "prompt_tokens": chat.prompt_tokens,
                "completion_tokens": chat.completion_tokens,
                "found": [f"{c.path} › {c.section_title} · {c.score:.2f}" for c in found],
                "sources": [f"{labels[label].path} › {labels[label].section_title}" for label in known],
                "history_messages": len(history_turns),
                # Сам переписанный запрос это слова клиента, в лог он не идёт.
                "query_rewritten": search_query != question,
                "profile_updates": sorted(k for k, v in parsed.profile_updates.model_dump().items() if v is not None),
                "lead_kind": parsed.lead.kind if parsed.lead else None,
                "needs_human": needs_human,
                "handoff_reason": reason,
            },
        )
        text = texts.client_handoff(s) if needs_human else parsed.reply.strip()
        return Answer(
            text=text,
            needs_human=needs_human,
            handoff_reason=reason,
            meta=meta,
            profile_updates=parsed.profile_updates.model_dump(),
            lead=parsed.lead.model_dump() if parsed.lead else None,
        )

    async def _search_query(self, question: str, memory: Memory, ids: dict[str, int | None]) -> str:
        """Вопрос, переписанный в самостоятельный запрос к базе знаний.

        Без этого «а если 24 занятия?» уйдёт в поиск как есть, и куски про
        IELTS не найдутся. На первом сообщении переписывать нечего.
        """
        s = self._s
        if not s.rewrite_query or (not memory.history and not memory.profile):
            return question
        try:
            result = await self._llm.chat(
                model=s.llm_model,
                messages=build_rewrite_messages(memory.profile, memory.history, question),
                json_schema=REWRITE_JSON_SCHEMA,
                schema_name=REWRITE_SCHEMA_NAME,
                max_tokens=200,
                reasoning_effort=s.llm_reasoning_effort,
                provider=s.llm_provider,
            )
            query = RewrittenQuery.model_validate_json(result.content).query.strip()
        except (LLMError, ValidationError) as exc:
            kind = exc.kind if isinstance(exc, LLMError) else "invalid_json"
            latency = exc.latency_ms if isinstance(exc, LLMError) else 0
            await self._log_call("rewrite", s.llm_model, latency, ids, error=kind)
            # Ищем по сырому сообщению: это хуже, но лучше, чем не ответить.
            return question
        await self._log_call(
            "rewrite", s.llm_model, result.latency_ms, ids,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        )
        return query or question

    def _fallback(self, reason: str, details: dict[str, Any]) -> Answer:
        log.warning("модель не ответила, клиенту нейтральный текст", extra={"reason": reason, **details})
        meta = {"model": self._s.llm_model, "needs_human": True, "handoff_reason": reason, **details}
        return Answer(
            text=texts.client_handoff(self._s), needs_human=True, handoff_reason=reason, meta=meta
        )

    @staticmethod
    def _source(chunk: FoundChunk) -> dict[str, Any]:
        return {"path": chunk.path, "section": chunk.section_title, "chunk_id": chunk.id}

    async def _log_call(
        self,
        purpose: str,
        model: str,
        latency_ms: int,
        ids: dict[str, int | None],
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        # Журнал расхода не должен ронять ответ клиенту.
        try:
            await self._repo.add_llm_call(
                purpose=purpose, model=model, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, error=error, **ids,
            )
        except DB_ERRORS as exc:
            log.error("не записан вызов модели", extra={"purpose": purpose, "error": type(exc).__name__})
