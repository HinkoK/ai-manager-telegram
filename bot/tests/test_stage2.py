"""Этап 2: база.

Проверяем обещания этапа: переписка ложится в таблицы, рестарт не даёт второго
ответа, эпизод закрывается по тишине, падение базы не роняет бота, а роль бота
не видит соседей по базе. Бот ходит в Postgres ролью school_bot_test с RLS,
как в продакшене, а проверки читают базу ролью postgres.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from scripts.migrate import apply_migrations
from tests.conftest import CLIENT_ID, TEST_ROLE, TEST_SCHEMA, wait_until, wait_until_async
from tests.fake_telegram import message_update

S = TEST_SCHEMA


async def batch_done(state) -> bool:
    """Telegram подтвердил offset, значит, батч обработан и записан целиком."""
    return await wait_until(lambda: state.pending == [])


async def test_conversation_is_saved(bot, db_admin):
    state, _server, settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="/start"))
    state.push(message_update(2, user_id=CLIENT_ID, text="сколько стоит пробный"))
    assert await wait_until(lambda: len(state.sent) == 2)
    assert await batch_done(state)

    client = await db_admin.fetchrow(f"select * from {S}.clients")
    assert client["telegram_user_id"] == CLIENT_ID
    assert client["first_name"] == "Тест"
    assert client["is_blocked_bot"] is False

    conversations = await db_admin.fetch(f"select * from {S}.conversations")
    assert len(conversations) == 1
    assert conversations[0]["status"] == "open"
    assert conversations[0]["state"] == "AI_ACTIVE"

    messages = await db_admin.fetch(f"select * from {S}.messages order by id")
    assert [m["role"] for m in messages] == ["client", "bot", "client", "bot"]
    assert messages[0]["text"] == "/start"
    assert settings.school_name in messages[1]["text"]
    assert messages[2]["text"] == "сколько стоит пробный"
    assert [m["telegram_message_id"] for m in messages] == [
        1, state.sent[0]["message_id"], 2, state.sent[1]["message_id"],
    ]
    assert {m["conversation_id"] for m in messages} == {conversations[0]["id"]}

    updates = await db_admin.fetch(f"select * from {S}.processed_updates order by update_id")
    assert [u["update_id"] for u in updates] == [1, 2]
    assert all(u["completed_at"] is not None for u in updates)


async def test_non_text_message_is_saved_with_kind(bot, db_admin):
    state, _server, _settings = bot
    state.push(
        message_update(
            1, user_id=CLIENT_ID, text=None,
            extra={"voice": {"file_id": "abc", "duration": 3, "file_unique_id": "u"}},
        )
    )
    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)

    row = await db_admin.fetchrow(f"select text, meta from {S}.messages where role = 'client'")
    assert row["text"] is None
    assert json.loads(row["meta"]) == {"kind": "voice"}


async def test_redelivered_update_after_restart_is_not_answered_again(fake_tg, start_bot, db_admin):
    state, _tg_server = fake_tg
    first, _settings = await start_bot()
    update = message_update(10, user_id=CLIENT_ID, text="сколько стоит")
    state.push(update)
    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)
    await first.stop()

    # Процесс перезапустился, а Telegram прислал тот же update ещё раз.
    state.push(dict(update))
    await start_bot()
    assert await batch_done(state)
    await asyncio.sleep(0.3)

    assert len(state.sent) == 1
    assert await db_admin.fetchval(f"select count(*) from {S}.messages") == 2


async def test_abandoned_claim_is_taken_again(fake_tg, start_bot, db_admin):
    """Процесс упал посреди обработки: заявка есть, завершения нет, и она старая."""
    state, _tg_server = fake_tg
    await db_admin.execute(
        f"insert into {S}.processed_updates (update_id, claimed_at) "
        "values (20, now() - interval '1 hour')"
    )
    await start_bot()
    state.push(message_update(20, user_id=CLIENT_ID, text="есть кто?"))

    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)
    assert await db_admin.fetchval(
        f"select completed_at is not null from {S}.processed_updates where update_id = 20"
    )


async def test_fresh_claim_is_waited_for_not_skipped(fake_tg, start_bot, db_admin):
    """При деплое старый контейнер ещё держит update, новый получил его повторно.

    Новый ждёт. Старый так и не завершил: заявка протухла, новый ответил сам.
    Молча пропустить нельзя, иначе клиент останется без ответа.
    """
    state, _tg_server = fake_tg
    await start_bot(update_claim_stale_sec=3.0)
    await db_admin.execute(f"insert into {S}.processed_updates (update_id) values (30)")
    state.push(message_update(30, user_id=CLIENT_ID, text="алло"))

    await asyncio.sleep(1.2)
    assert state.sent == []
    assert await wait_until(lambda: len(state.sent) == 1, timeout=8.0)


async def test_episode_closes_after_silence(bot, db_admin):
    state, _server, _settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="привет"))
    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)

    # Сутки с лишним тишины: сдвигаем последнее сообщение эпизода в прошлое.
    await db_admin.execute(
        f"update {S}.conversations set last_message_at = now() - interval '25 hours'"
    )
    state.push(message_update(2, user_id=CLIENT_ID, text="я снова тут"))
    assert await wait_until(lambda: len(state.sent) == 2)
    assert await batch_done(state)

    conversations = await db_admin.fetch(f"select * from {S}.conversations order by id")
    assert [c["status"] for c in conversations] == ["closed", "open"]
    assert conversations[0]["closed_at"] is not None
    old_id, new_id = conversations[0]["id"], conversations[1]["id"]

    messages = await db_admin.fetch(f"select conversation_id, role from {S}.messages order by id")
    assert [(m["conversation_id"], m["role"]) for m in messages] == [
        (old_id, "client"), (old_id, "bot"), (new_id, "client"), (new_id, "bot"),
    ]
    assert await db_admin.fetchval(f"select count(*) from {S}.clients") == 1

    event = await db_admin.fetchrow(f"select * from {S}.events where type = 'conversation_closed'")
    assert event["conversation_id"] == old_id
    assert json.loads(event["payload"]) == {"reason": "silence"}


async def test_database_allows_one_open_episode_per_client(bot, db_admin):
    state, _server, _settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="привет"))
    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)

    client_id = await db_admin.fetchval(f"select id from {S}.clients")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db_admin.execute(f"insert into {S}.conversations (client_id) values ($1)", client_id)


async def test_bot_is_silent_while_owner_holds_conversation(bot, db_admin):
    """Клиенту бот не пишет, но сообщение уходит владельцу: ретрансляция с этапа 6."""
    state, _server, _settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="позовите человека"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    await db_admin.execute(f"update {S}.conversations set state = 'HUMAN_ACTIVE'")
    state.push(message_update(2, user_id=CLIENT_ID, text="жду ответа"))
    assert await batch_done(state)
    await asyncio.sleep(0.3)

    assert len(state.texts_to(CLIENT_ID)) == 1
    roles = await db_admin.fetch(f"select role from {S}.messages order by id")
    assert [r["role"] for r in roles] == ["client", "bot", "client"]


async def test_blocked_bot_is_flagged_and_cleared(bot, db_admin):
    state, _server, _settings = bot
    state.forbidden_chats.add(CLIENT_ID)
    state.push(message_update(1, user_id=CLIENT_ID, text="привет"))
    assert await batch_done(state)

    async def flagged() -> bool:
        return bool(await db_admin.fetchval(f"select is_blocked_bot from {S}.clients"))

    assert await wait_until_async(flagged)
    assert await db_admin.fetchval(f"select count(*) from {S}.events where type = 'bot_blocked'") == 1
    assert await db_admin.fetchval(f"select count(*) from {S}.messages where role = 'bot'") == 0

    # Клиент разблокировал бота и написал снова.
    state.forbidden_chats.clear()
    state.push(message_update(2, user_id=CLIENT_ID, text="я вернулся"))
    assert await wait_until(lambda: len(state.sent) == 1)
    assert await batch_done(state)
    assert not await flagged()


async def test_database_outage_does_not_kill_bot(bot, db_admin):
    state, _server, _settings = bot

    # База отказывает боту: новые входы запрещены, открытые соединения обрываются.
    await db_admin.execute(f"alter role {TEST_ROLE} nologin")
    try:
        await db_admin.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where usename = $1", TEST_ROLE
        )

        async def no_bot_connections() -> bool:
            count = await db_admin.fetchval(
                "select count(*) from pg_stat_activity where usename = $1", TEST_ROLE
            )
            return count == 0

        assert await wait_until_async(no_bot_connections)

        state.push(message_update(1, user_id=CLIENT_ID, text="сколько стоит"))
        assert await wait_until(lambda: len(state.sent) == 1)
        assert "технический сбой" in state.sent[0]["text"]
        assert await batch_done(state)
    finally:
        await db_admin.execute(f"alter role {TEST_ROLE} login")

    # База вернулась: бот отвечает как обычно и снова пишет переписку.
    state.push(message_update(2, user_id=CLIENT_ID, text="сколько стоит"))
    assert await wait_until(lambda: len(state.sent) == 2)
    assert "Фейковый ответ модели" in state.sent[1]["text"]
    assert await batch_done(state)
    assert await db_admin.fetchval(f"select count(*) from {S}.messages") == 2


async def test_bot_role_cannot_read_neighbour_schemas(database, db_admin):
    """В одной базе с ботом могут жить другие проекты: их таблицы роли бота закрыты."""
    await db_admin.execute("create schema if not exists shop")
    await db_admin.execute("create table if not exists shop.orders (id int)")
    await db_admin.execute("create table if not exists public.site_pages (id int)")
    conn = await asyncpg.connect(database.bot_url)
    try:
        # search_path роли указывает на свою схему.
        assert await conn.fetchval("select count(*) from clients") == 0
        for table in ("shop.orders", "public.site_pages"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(f"select 1 from {table}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("create table public.bot_probe (id int)")
    finally:
        await conn.close()
        await db_admin.execute("drop schema shop cascade")
        await db_admin.execute("drop table public.site_pages")


async def test_migrations_are_applied_once(database, db_admin):
    again = await apply_migrations(db_admin, TEST_SCHEMA, TEST_ROLE, report=lambda _line: None)
    assert again == []
