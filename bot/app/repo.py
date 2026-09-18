"""Слой доступа к данным. Весь SQL живёт здесь, обработчики его не видят.

Имя схемы приходит из настроек и подставляется в запросы текстом, поэтому оно
проверяется в db.check_identifier: тесты гоняются в school_test, продакшен в
school, и SQL при этом один и тот же.

Методы, которые принимают conn, работают внутри транзакции вызывающего. Методы
без conn берут соединение из пула сами. У части методов conn необязательный:
их зовут и отдельно, и внутри чужой транзакции.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import asyncpg

from .db import Database

log = logging.getLogger(__name__)

# Колонки заявки, которые бот вправе менять. Статусом и заметками займётся
# кабинет на этапе 7.
LEAD_COLUMNS = frozenset({
    "goal", "level", "format", "timezone", "preferred_time",
    "company", "team_size", "sphere", "notes", "is_qualified",
})


@dataclass(frozen=True)
class Client:
    id: int
    telegram_user_id: int
    is_banned: bool


@dataclass(frozen=True)
class Conversation:
    id: int
    state: str
    status: str


@dataclass(frozen=True)
class StoredDocument:
    id: int
    content_hash: str
    embedding_model: str


@dataclass(frozen=True)
class FoundChunk:
    id: int
    path: str
    section_title: str
    content: str
    score: float


def vector_literal(vector: list[float]) -> str:
    """pgvector принимает вектор текстом '[0.1,0.2]'. Приводим на стороне базы:
    у asyncpg нет кодека для типа vector, а текст он передаёт без вопросов."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class Repo:
    def __init__(self, db: Database) -> None:
        self._db = db
        s = db.schema
        self.q_claim_update = f"""
            insert into {s}.processed_updates as pu (update_id)
            values ($1)
            on conflict (update_id) do update
               set claimed_at = now()
             where pu.completed_at is null
               and pu.claimed_at < now() - make_interval(secs => $2::double precision)
            returning pu.update_id
        """
        self.q_update_completed = f"""
            select completed_at is not null from {s}.processed_updates where update_id = $1
        """
        self.q_complete_update = f"""
            update {s}.processed_updates set completed_at = now() where update_id = $1
        """
        self.q_purge_updates = f"""
            delete from {s}.processed_updates
             where claimed_at < now() - make_interval(days => $1::int)
        """
        # Клиент написал, значит, бот у него не заблокирован: флаг снимаем.
        self.q_upsert_client = f"""
            insert into {s}.clients (telegram_user_id, username, first_name, language_code)
            values ($1, $2, $3, $4)
            on conflict (telegram_user_id) do update
               set username       = excluded.username,
                   first_name     = excluded.first_name,
                   language_code  = excluded.language_code,
                   is_blocked_bot = false,
                   last_seen_at   = now()
            returning id, telegram_user_id, is_banned
        """
        self.q_mark_blocked = f"""
            update {s}.clients set is_blocked_bot = true
             where telegram_user_id = $1
            returning id
        """
        self.q_close_stale = f"""
            update {s}.conversations
               set status = 'closed', closed_at = now()
             where client_id = $1
               and status = 'open'
               and last_message_at < now() - make_interval(secs => $2::double precision)
            returning id
        """
        self.q_open_conversation = f"""
            select id, state, status from {s}.conversations
             where client_id = $1 and status = 'open'
        """
        self.q_start_conversation = f"""
            insert into {s}.conversations (client_id) values ($1)
            returning id, state, status
        """
        self.q_insert_message = f"""
            insert into {s}.messages
                (conversation_id, client_id, role, text, telegram_message_id, meta)
            values ($1, $2, $3, $4, $5, $6::jsonb)
            returning id
        """
        self.q_touch_conversation = f"""
            update {s}.conversations set last_message_at = now() where id = $1
        """
        self.q_insert_event = f"""
            insert into {s}.events (conversation_id, client_id, type, payload)
            values ($1, $2, $3, $4::jsonb)
        """
        self.q_client_memory = f"""
            select profile, summary, summary_message_id from {s}.clients where id = $1
        """
        # $2 это текущее сообщение: оно уходит модели отдельной репликой.
        self.q_recent_messages = f"""
            select id, role, text from {s}.messages
             where conversation_id = $1 and id <> $2 and text is not null
             order by id desc
             limit $3
        """
        # $3 это верхняя граница: при открытии нового эпизода обобщаем то, что
        # было до нового вопроса, а сам вопрос попадёт в следующее резюме.
        self.q_messages_since = f"""
            select id, role, text from {s}.messages
             where client_id = $1 and id > $2 and text is not null
               and ($3::bigint is null or id < $3)
             order by id
             limit $4
        """
        self.q_count_since = f"""
            select count(*) as messages, max(id) as last_id from {s}.messages
             where client_id = $1 and id > $2
               and ($3::bigint is null or id < $3)
        """
        self.q_update_profile = f"""
            update {s}.clients set profile = profile || $2::jsonb where id = $1
        """
        self.q_save_summary = f"""
            update {s}.clients
               set summary = $2, summary_message_id = $3, summary_updated_at = now()
             where id = $1
        """
        self.q_client_message_counts = f"""
            select count(*) filter (where created_at > now() - interval '1 minute') as per_minute,
                   count(*) filter (where created_at > now() - interval '1 day') as per_day
              from {s}.messages
             where client_id = $1 and role = 'client'
        """
        self.q_tokens_last_day = f"""
            select coalesce(sum(coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)), 0)
              from {s}.llm_calls
             where created_at > now() - interval '1 day'
        """
        self.q_event_within = f"""
            select exists(
              select 1 from {s}.events
               where type = $1 and created_at > now() - make_interval(hours => $2::int)
            )
        """
        self.q_set_banned = f"""
            update {s}.clients
               set is_banned = $2,
                   banned_at = case when $2 then now() else null end,
                   ban_reason = case when $2 then $3 else null end
             where id = $1
            returning id
        """
        self.q_create_session = f"""
            insert into {s}.owner_sessions (token_hash, expires_at, user_agent)
            values ($1, now() + make_interval(days => $2::int), $3)
            returning id
        """
        self.q_session_alive = f"""
            select id from {s}.owner_sessions
             where token_hash = $1 and revoked_at is null and expires_at > now()
        """
        self.q_revoke_session = f"""
            update {s}.owner_sessions set revoked_at = now()
             where token_hash = $1 and revoked_at is null
        """
        self.q_purge_sessions = f"""
            delete from {s}.owner_sessions where expires_at < now() - interval '30 days'
        """
        # Список разговоров кабинета: последнее сообщение и непрочитанность.
        self.q_conversations = f"""
            select v.id, v.state, v.status, v.needs_attention, v.handoff_reason,
                   v.last_message_at, v.owner_seen_at,
                   (v.owner_seen_at is null or v.last_message_at > v.owner_seen_at) as unread,
                   c.id as client_id, c.telegram_user_id, c.username, c.first_name,
                   (select m.text from {s}.messages m
                     where m.conversation_id = v.id and m.text is not null
                     order by m.id desc limit 1) as last_text,
                   (select count(*) from {s}.leads l
                     where l.client_id = c.id and l.status in ('new', 'in_progress')) as open_leads
              from {s}.conversations v
              join {s}.clients c on c.id = v.client_id
             where ($1::text is null
                    or ($1 = 'attention' and v.needs_attention)
                    or ($1 = 'human' and v.state in ('HUMAN_REQUESTED', 'HUMAN_ACTIVE'))
                    or ($1 = 'bot' and v.state = 'AI_ACTIVE'))
             order by v.last_message_at desc
             limit $2
        """
        self.q_conversation = f"""
            select v.id, v.state, v.status, v.needs_attention, v.handoff_reason,
                   v.handoff_at, v.returned_at, v.started_at, v.last_message_at,
                   c.id as client_id, c.telegram_user_id, c.username, c.first_name,
                   c.profile, c.summary, c.created_at as client_created_at
              from {s}.conversations v
              join {s}.clients c on c.id = v.client_id
             where v.id = $1
        """
        # В переписке кабинета видны и события: передача, возврат, напоминание.
        self.q_conversation_events = f"""
            select type, payload, created_at from {s}.events
             where conversation_id = $1
               and type in ('handoff_started', 'handoff_returned', 'owner_reminded')
             order by id
        """
        self.q_conversation_messages = f"""
            select id, role, text, meta, created_at from {s}.messages
             where conversation_id = $1 order by id
        """
        self.q_mark_seen = f"""
            update {s}.conversations
               set owner_seen_at = now(), needs_attention = false
             where id = $1
        """
        self.q_leads_list = f"""
            select l.*, c.username, c.first_name, c.telegram_user_id
              from {s}.leads l
              join {s}.clients c on c.id = l.client_id
             where ($1::text is null or l.status = $1)
             order by l.updated_at desc
             limit $2
        """
        self.q_leads_of_client = f"""
            select l.*, c.username, c.first_name, c.telegram_user_id
              from {s}.leads l
              join {s}.clients c on c.id = l.client_id
             where l.client_id = $1
             order by l.id desc
        """
        self.q_update_lead_card = f"""
            update {s}.leads
               set status = coalesce($2, status), notes = coalesce($3, notes), updated_at = now()
             where id = $1
            returning *
        """
        self.q_start_handoff = f"""
            update {s}.conversations
               set state = 'HUMAN_REQUESTED', handoff_reason = $2, handoff_at = now(),
                   reminded_at = null, returned_at = null
             where id = $1 and state = 'AI_ACTIVE'
            returning id
        """
        self.q_owner_took = f"""
            update {s}.conversations set state = 'HUMAN_ACTIVE' where id = $1
        """
        self.q_return_to_bot = f"""
            update {s}.conversations
               set state = 'AI_ACTIVE', returned_at = now(), needs_attention = $2
             where id = $1 and state <> 'AI_ACTIVE'
            returning id
        """
        self.q_set_owner_relay = f"""
            update {s}.messages set owner_relay_message_id = $2 where id = $1
        """
        # Реплай владельца на любое сообщение того разговора находит разговор.
        self.q_conversation_by_relay = f"""
            select v.id, v.state, v.status, c.telegram_user_id, c.id as client_id
              from {s}.messages m
              join {s}.conversations v on v.id = m.conversation_id
              join {s}.clients c on c.id = m.client_id
             where m.owner_relay_message_id = $1
             order by m.id desc limit 1
        """
        self.q_human_conversations = f"""
            select v.id, v.state, v.handoff_reason, v.handoff_at, v.reminded_at,
                   c.id as client_id, c.telegram_user_id, c.username, c.first_name
              from {s}.conversations v
              join {s}.clients c on c.id = v.client_id
             where v.status = 'open' and v.state = any($1::text[])
             order by v.handoff_at
        """
        self.q_status_counts = f"""
            select
              (select count(*) from {s}.conversations
                where status = 'open' and state in ('HUMAN_REQUESTED', 'HUMAN_ACTIVE')) as human,
              (select count(*) from {s}.conversations
                where status = 'open' and state = 'HUMAN_REQUESTED') as requested,
              (select count(*) from {s}.leads where status = 'new') as new_leads
        """
        self.q_mark_reminded = f"""
            update {s}.conversations set reminded_at = now() where id = $1
        """
        self.q_active_lead = f"""
            select * from {s}.leads
             where client_id = $1 and kind = $2 and status in ('new', 'in_progress')
             order by id desc limit 1
        """
        self.q_create_lead = f"""
            insert into {s}.leads (client_id, conversation_id, kind)
            values ($1, $2, $3)
            returning *
        """
        self.q_mark_lead_notified = f"""
            update {s}.leads set {{column}} = now(), updated_at = now() where id = $1
        """
        self.q_list_documents = f"""
            select id, path, content_hash, embedding_model from {s}.knowledge_documents
        """
        self.q_upsert_document = f"""
            insert into {s}.knowledge_documents (path, title, content_hash, embedding_model, chunk_count)
            values ($1, $2, $3, $4, $5)
            on conflict (path) do update
               set title           = excluded.title,
                   content_hash    = excluded.content_hash,
                   embedding_model = excluded.embedding_model,
                   chunk_count     = excluded.chunk_count,
                   updated_at      = now()
            returning id
        """
        self.q_delete_chunks = f"""
            delete from {s}.knowledge_chunks where document_id = $1
        """
        self.q_insert_chunk = f"""
            insert into {s}.knowledge_chunks
                (document_id, chunk_index, section_title, content, embedding)
            values ($1, $2, $3, $4, $5::text::public.vector)
        """
        self.q_delete_document = f"""
            delete from {s}.knowledge_documents where path = $1
        """
        # Оператор с явной схемой: у роли бота в пути поиска только своя схема.
        self.q_search = f"""
            select c.id, d.path, c.section_title, c.content,
                   1 - (c.embedding operator(public.<=>) $1::text::public.vector) as score
              from {s}.knowledge_chunks c
              join {s}.knowledge_documents d on d.id = c.document_id
             where d.path <> $3
             order by c.embedding operator(public.<=>) $1::text::public.vector
             limit $2
        """
        self.q_document_chunks = f"""
            select c.id, d.path, c.section_title, c.content, 1.0::float8 as score
              from {s}.knowledge_chunks c
              join {s}.knowledge_documents d on d.id = c.document_id
             where d.path = $1
             order by c.chunk_index
        """
        # У pgvector размерность колонки хранится в atttypmod.
        self.q_embedding_column = f"""
            select a.atttypmod
              from pg_attribute a
             where a.attrelid = to_regclass('{s}.knowledge_chunks')
               and a.attname = 'embedding'
               and a.atttypid = to_regtype('public.vector')
               and not a.attisdropped
        """
        self.q_knowledge_models = f"""
            select embedding_model, count(*) as documents
              from {s}.knowledge_documents
             group by embedding_model
        """
        self.q_insert_llm_call = f"""
            insert into {s}.llm_calls
                (conversation_id, message_id, purpose, model, prompt_tokens,
                 completion_tokens, latency_ms, error)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
        """

    @asynccontextmanager
    async def _use(self, conn: asyncpg.Connection | None) -> AsyncIterator[asyncpg.Connection]:
        """Чужое соединение, если дали, иначе своё из пула."""
        if conn is not None:
            yield conn
            return
        async with self._db.acquire() as own:
            yield own

    # --- идемпотентность ----------------------------------------------------

    async def claim_update(self, update_id: int, stale_after_sec: float) -> bool:
        """True, если update наш. False, если его уже обработали или обрабатывают.

        Заявка ставится до обработки, завершение после отправки ответа. Заявка
        без завершения старше порога считается брошенной и берётся снова.
        """
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_claim_update, update_id, stale_after_sec)
        return row is not None

    async def update_completed(self, update_id: int) -> bool:
        """Отличает «уже обработан» от «заявку держит кто-то ещё»."""
        async with self._db.acquire() as conn:
            done = await conn.fetchval(self.q_update_completed, update_id)
        return bool(done)

    async def complete_update(self, update_id: int, conn: asyncpg.Connection | None = None) -> None:
        async with self._use(conn) as c:
            await c.execute(self.q_complete_update, update_id)

    async def purge_old_updates(self, days: int) -> int:
        async with self._db.acquire() as conn:
            result = await conn.execute(self.q_purge_updates, days)
        return int(result.rsplit(" ", 1)[-1] or 0)

    # --- клиенты и эпизоды --------------------------------------------------

    async def upsert_client(self, conn: asyncpg.Connection, sender: dict[str, Any]) -> Client:
        row = await conn.fetchrow(
            self.q_upsert_client,
            int(sender["id"]),
            sender.get("username"),
            sender.get("first_name"),
            sender.get("language_code"),
        )
        return Client(id=row["id"], telegram_user_id=row["telegram_user_id"], is_banned=row["is_banned"])

    async def mark_blocked(self, telegram_user_id: int) -> None:
        """Telegram ответил 403: клиент заблокировал бота. Флаг и запись в журнал."""
        async with self._db.tx() as conn:
            client_id = await conn.fetchval(self.q_mark_blocked, telegram_user_id)
            if client_id is not None:
                await self.add_event("bot_blocked", client_id=client_id, conn=conn)

    async def resolve_conversation(
        self, conn: asyncpg.Connection, client_id: int, episode_ttl_sec: float
    ) -> tuple[Conversation, bool]:
        """Возвращает открытый эпизод и флаг «он только что открыт».

        Закрытие ленивое: протухший эпизод закрывается в тот момент, когда клиент
        написал снова. Фоновой задачи нет, поэтому эпизод, в который не вернулись,
        остаётся открытым до следующего сообщения.
        """
        closed = await conn.fetchval(self.q_close_stale, client_id, episode_ttl_sec)
        if closed is not None:
            log.info("эпизод закрыт по тишине", extra={"conversation_id": closed})
            await self.add_event(
                "conversation_closed",
                conversation_id=closed,
                client_id=client_id,
                payload={"reason": "silence"},
                conn=conn,
            )
        row = await conn.fetchrow(self.q_open_conversation, client_id)
        if row is not None:
            return Conversation(id=row["id"], state=row["state"], status=row["status"]), False
        row = await conn.fetchrow(self.q_start_conversation, client_id)
        return Conversation(id=row["id"], state=row["state"], status=row["status"]), True

    async def insert_message(
        self,
        conn: asyncpg.Connection,
        *,
        conversation_id: int,
        client_id: int,
        role: str,
        text: str | None,
        telegram_message_id: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        return await conn.fetchval(
            self.q_insert_message,
            conversation_id,
            client_id,
            role,
            text,
            telegram_message_id,
            json.dumps(meta or {}, ensure_ascii=False),
        )

    async def touch_conversation(self, conn: asyncpg.Connection, conversation_id: int) -> None:
        await conn.execute(self.q_touch_conversation, conversation_id)

    async def add_event(
        self,
        type_: str,
        *,
        conversation_id: int | None = None,
        client_id: int | None = None,
        payload: dict[str, Any] | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        async with self._use(conn) as c:
            await c.execute(
                self.q_insert_event,
                conversation_id,
                client_id,
                type_,
                json.dumps(payload or {}, ensure_ascii=False),
            )

    # --- память клиента ------------------------------------------------------

    async def client_memory(self, client_id: int) -> tuple[dict[str, Any], str | None, int | None]:
        """Профиль, резюме и до какого сообщения резюме доведено."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_client_memory, client_id)
        if row is None:
            return {}, None, None
        return json.loads(row["profile"] or "{}"), row["summary"], row["summary_message_id"]

    async def recent_messages(
        self, conversation_id: int, limit: int, exclude_message_id: int | None = None
    ) -> list[tuple[str, str]]:
        """Последние реплики эпизода по возрастанию: роль и текст."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                self.q_recent_messages, conversation_id, exclude_message_id or 0, limit
            )
        return [(r["role"], r["text"]) for r in reversed(rows)]

    async def messages_since(
        self,
        client_id: int,
        after_message_id: int | None,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[tuple[str, str]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                self.q_messages_since, client_id, after_message_id or 0, before_message_id, limit
            )
        return [(r["role"], r["text"]) for r in rows]

    async def count_since(
        self, client_id: int, after_message_id: int | None, before_message_id: int | None = None
    ) -> tuple[int, int | None]:
        """Сколько сообщений накопилось после резюме и id последнего из них."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                self.q_count_since, client_id, after_message_id or 0, before_message_id
            )
        return int(row["messages"]), row["last_id"]

    async def update_profile(
        self, client_id: int, patch: dict[str, Any], conn: asyncpg.Connection | None = None
    ) -> None:
        if not patch:
            return
        async with self._use(conn) as c:
            await c.execute(self.q_update_profile, client_id, json.dumps(patch, ensure_ascii=False))

    async def save_summary(self, client_id: int, summary: str, message_id: int | None) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(self.q_save_summary, client_id, summary, message_id)

    # --- лимиты и бан ----------------------------------------------------------

    async def client_message_counts(self, client_id: int) -> tuple[int, int]:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_client_message_counts, client_id)
        return int(row["per_minute"]), int(row["per_day"])

    async def tokens_last_day(self) -> int:
        async with self._db.acquire() as conn:
            return int(await conn.fetchval(self.q_tokens_last_day))

    async def event_within(self, type_: str, hours: int) -> bool:
        async with self._db.acquire() as conn:
            return bool(await conn.fetchval(self.q_event_within, type_, hours))

    async def set_banned(self, client_id: int, banned: bool, reason: str | None = None) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchval(self.q_set_banned, client_id, banned, reason)
        return row is not None

    # --- кабинет владельца ----------------------------------------------------

    async def create_session(self, token_hash: str, days: int, user_agent: str | None) -> int:
        async with self._db.acquire() as conn:
            return await conn.fetchval(self.q_create_session, token_hash, days, user_agent)

    async def session_alive(self, token_hash: str) -> bool:
        async with self._db.acquire() as conn:
            return await conn.fetchval(self.q_session_alive, token_hash) is not None

    async def revoke_session(self, token_hash: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(self.q_revoke_session, token_hash)

    async def purge_sessions(self) -> int:
        async with self._db.acquire() as conn:
            result = await conn.execute(self.q_purge_sessions)
        return int(result.rsplit(" ", 1)[-1] or 0)

    async def conversations(self, filter_: str | None, limit: int) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_conversations, filter_, limit)
        return [dict(r) for r in rows]

    async def conversation(self, conversation_id: int) -> dict[str, Any] | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_conversation, conversation_id)
        return dict(row) if row is not None else None

    async def conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_conversation_messages, conversation_id)
        return [dict(r) for r in rows]

    async def conversation_events(self, conversation_id: int) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_conversation_events, conversation_id)
        return [dict(r) for r in rows]

    async def mark_seen(self, conversation_id: int) -> None:
        """Владелец открыл разговор: он прочитан и больше не требует внимания."""
        async with self._db.acquire() as conn:
            await conn.execute(self.q_mark_seen, conversation_id)

    async def leads(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_leads_list, status, limit)
        return [dict(r) for r in rows]

    async def leads_of_client(self, client_id: int) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_leads_of_client, client_id)
        return [dict(r) for r in rows]

    async def update_lead_card(
        self, lead_id: int, status: str | None, notes: str | None
    ) -> dict[str, Any] | None:
        """Статус и заметку меняет только кабинет, бот сюда не пишет."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_update_lead_card, lead_id, status, notes)
        return dict(row) if row is not None else None

    # --- передача владельцу ---------------------------------------------------

    async def start_handoff(
        self, conn: asyncpg.Connection, conversation_id: int, reason: str | None
    ) -> bool:
        """True, если разговор только что перешёл к владельцу."""
        return await conn.fetchval(self.q_start_handoff, conversation_id, reason) is not None

    async def owner_took_conversation(self, conversation_id: int) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(self.q_owner_took, conversation_id)

    async def return_to_bot(self, conversation_id: int, needs_attention: bool = False) -> bool:
        async with self._db.acquire() as conn:
            row = await conn.fetchval(self.q_return_to_bot, conversation_id, needs_attention)
        return row is not None

    async def set_owner_relay(
        self, message_id: int, owner_message_id: int, conn: asyncpg.Connection | None = None
    ) -> None:
        async with self._use(conn) as c:
            await c.execute(self.q_set_owner_relay, message_id, owner_message_id)

    async def conversation_by_relay(self, owner_message_id: int) -> dict[str, Any] | None:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_conversation_by_relay, owner_message_id)
        return dict(row) if row is not None else None

    async def human_conversations(self, states: list[str]) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_human_conversations, states)
        return [dict(r) for r in rows]

    async def status_counts(self) -> tuple[int, int, int]:
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(self.q_status_counts)
        return int(row["human"]), int(row["requested"]), int(row["new_leads"])

    async def mark_reminded(self, conversation_id: int) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(self.q_mark_reminded, conversation_id)

    # --- заявки --------------------------------------------------------------

    async def active_lead(
        self, conn: asyncpg.Connection, client_id: int, kind: str
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(self.q_active_lead, client_id, kind)
        return dict(row) if row is not None else None

    async def create_lead(
        self, conn: asyncpg.Connection, client_id: int, conversation_id: int, kind: str
    ) -> dict[str, Any]:
        row = await conn.fetchrow(self.q_create_lead, client_id, conversation_id, kind)
        return dict(row)

    async def update_lead(
        self, conn: asyncpg.Connection, lead_id: int, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Имена колонок приходят из белого списка leads.py, не из ответа модели."""
        unknown = set(patch) - LEAD_COLUMNS
        if unknown:
            raise ValueError(f"недопустимые поля заявки: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ${i}" for i, name in enumerate(patch, start=2))
        row = await conn.fetchrow(
            f"update {self._db.schema}.leads set {assignments}, updated_at = now() "
            "where id = $1 returning *",
            lead_id,
            *patch.values(),
        )
        return dict(row)

    async def mark_lead_notified(self, lead_id: int, column: str) -> None:
        if column not in ("notified_at", "qualified_notified_at"):
            raise ValueError(f"неизвестная отметка уведомления: {column}")
        async with self._db.acquire() as conn:
            await conn.execute(self.q_mark_lead_notified.format(column=column), lead_id)

    # --- база знаний ---------------------------------------------------------

    async def list_documents(self) -> dict[str, StoredDocument]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_list_documents)
        return {
            r["path"]: StoredDocument(r["id"], r["content_hash"], r["embedding_model"]) for r in rows
        }

    async def replace_document(
        self,
        *,
        path: str,
        title: str,
        content_hash: str,
        embedding_model: str,
        chunks: list[tuple[int, str, str, list[float]]],
    ) -> None:
        """Документ и все его куски заменяются одной транзакцией: поиск никогда
        не видит файл наполовину загруженным."""
        async with self._db.tx() as conn:
            document_id = await conn.fetchval(
                self.q_upsert_document, path, title, content_hash, embedding_model, len(chunks)
            )
            await conn.execute(self.q_delete_chunks, document_id)
            await conn.executemany(
                self.q_insert_chunk,
                [
                    (document_id, index, section, content, vector_literal(vector))
                    for index, section, content, vector in chunks
                ],
            )

    async def delete_document(self, path: str) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(self.q_delete_document, path)

    async def search_chunks(
        self, vector: list[float], top_k: int, exclude_path: str
    ) -> list[FoundChunk]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_search, vector_literal(vector), top_k, exclude_path)
        return [self._found(r) for r in rows]

    async def document_chunks(self, path: str) -> list[FoundChunk]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_document_chunks, path)
        return [self._found(r) for r in rows]

    @staticmethod
    def _found(row: asyncpg.Record) -> FoundChunk:
        return FoundChunk(
            id=row["id"],
            path=row["path"],
            section_title=row["section_title"],
            content=row["content"],
            score=float(row["score"]),
        )

    async def embedding_column_dim(self) -> int | None:
        """Размерность колонки эмбеддингов. None, если миграция 0003 не применена."""
        async with self._db.acquire() as conn:
            return await conn.fetchval(self.q_embedding_column)

    async def knowledge_models(self) -> dict[str, int]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(self.q_knowledge_models)
        return {r["embedding_model"]: r["documents"] for r in rows}

    # --- расход модели ------------------------------------------------------

    async def add_llm_call(
        self,
        *,
        purpose: str,
        model: str,
        latency_ms: int,
        conversation_id: int | None = None,
        message_id: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._db.acquire() as conn:
            await conn.execute(
                self.q_insert_llm_call,
                conversation_id,
                message_id,
                purpose,
                model,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                error,
            )

    # --- удобные обёртки ----------------------------------------------------

    def tx(self):
        return self._db.tx()
