"""Этап 8: безопасность.

Лимиты на клиента, дневной бюджет модели, бан, защита входа в кабинет,
мутации только со своего сайта, размер тела запроса, секреты в трассировках
и права роли бота на учёт миграций.
"""

from __future__ import annotations

import asyncio
import io
import logging

import asyncpg
import httpx
import pytest

from app import texts
from app.auth import hash_password
from app.logging_setup import JsonFormatter, SecretRedactor
from tests.conftest import CLIENT_ID, OWNER_ID, TEST_SCHEMA, wait_until
from tests.fake_llm import EMPTY_PROFILE_UPDATES
from tests.fake_telegram import message_update

S = TEST_SCHEMA
PASSWORD = "cabinet-password-123"


def plain_reply(text: str = "Ответ бота") -> dict:
    return {
        "sources": [], "needs_human": False, "handoff_reason": None, "is_smalltalk": True,
        "reply": text, "profile_updates": EMPTY_PROFILE_UPDATES, "lead": None,
    }


def handoff_reply() -> dict:
    return {
        "sources": [], "needs_human": True, "handoff_reason": "client_request",
        "is_smalltalk": False, "reply": "", "profile_updates": EMPTY_PROFILE_UPDATES, "lead": None,
    }


async def settle(state) -> None:
    assert await wait_until(lambda: state.pending == [])
    await asyncio.sleep(0.2)


# --- лимиты на клиента --------------------------------------------------------


async def test_rate_limit_per_minute_pauses_once(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    _server, settings = await start_bot(client_msgs_per_minute=3)
    llm_state.replies += [plain_reply(f"Ответ {i}") for i in range(1, 4)]

    for i in range(1, 6):
        state.push(message_update(i, user_id=CLIENT_ID, text=f"вопрос {i}"))
    await settle(state)

    replies = state.texts_to(CLIENT_ID)
    assert replies[:3] == ["Ответ 1", "Ответ 2", "Ответ 3"]
    # Четвёртое получило паузу, пятое уже ничего: одна пауза на окно.
    assert replies[3:] == [texts.client_rate_limited(settings, "minute")]
    assert len(llm_state.answer_requests) == 3  # модель лишний раз не звали
    assert state.texts_to(OWNER_ID) == []  # владелец не заспамлен
    assert await db_admin.fetchval(f"select count(*) from {S}.messages where role = 'client'") == 5
    assert await db_admin.fetchval(f"select count(*) from {S}.events where type = 'rate_limited'") == 1


async def test_rate_limit_per_day(fake_tg, llm, start_bot):
    state, _tg = fake_tg
    llm_state, _ = llm
    _server, settings = await start_bot(client_msgs_per_minute=0, client_msgs_per_day=2)
    llm_state.replies += [plain_reply("Ответ 1"), plain_reply("Ответ 2")]

    for i in range(1, 4):
        state.push(message_update(i, user_id=CLIENT_ID, text=f"вопрос {i}"))
    await settle(state)

    assert state.texts_to(CLIENT_ID)[-1] == texts.client_rate_limited(settings, "day")
    assert len(llm_state.answer_requests) == 2


async def test_too_long_message_is_not_sent_to_model(fake_tg, llm, start_bot):
    state, _tg = fake_tg
    llm_state, _ = llm
    _server, settings = await start_bot(client_max_message_chars=50)

    state.push(message_update(1, user_id=CLIENT_ID, text="а" * 51))
    await settle(state)

    assert state.texts_to(CLIENT_ID) == [texts.client_too_long(settings, 50)]
    assert llm_state.chat_requests == [] and llm_state.embed_requests == []


# --- бан ---------------------------------------------------------------------


async def test_owner_bans_and_unbans_by_reply(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [handoff_reply(), plain_reply("Снова отвечаю")]

    state.push(message_update(1, user_id=CLIENT_ID, text="позовите человека"))
    await settle(state)
    card_id = state.sent[-1]["message_id"]

    state.push(message_update(2, user_id=OWNER_ID, text="/ban",
                              extra={"reply_to_message": {"message_id": card_id}}))
    await settle(state)
    assert "заблокирован" in state.texts_to(OWNER_ID)[-1]
    assert await db_admin.fetchval(f"select is_banned from {S}.clients") is True

    # Забаненный пишет: ни ответа, ни ретрансляции владельцу, ни вызова модели.
    owner_before, client_before = len(state.texts_to(OWNER_ID)), len(state.texts_to(CLIENT_ID))
    state.push(message_update(3, user_id=CLIENT_ID, text="эй, вы тут?"))
    await settle(state)
    assert len(state.texts_to(OWNER_ID)) == owner_before
    assert len(state.texts_to(CLIENT_ID)) == client_before
    assert len(llm_state.answer_requests) == 1

    state.push(message_update(4, user_id=OWNER_ID, text="/unban",
                              extra={"reply_to_message": {"message_id": card_id}}))
    await settle(state)
    assert await db_admin.fetchval(f"select is_banned from {S}.clients") is False
    types = [r["type"] for r in await db_admin.fetch(f"select type from {S}.events order by id")]
    assert "client_banned" in types and "client_unbanned" in types


# --- бюджет ------------------------------------------------------------------


async def test_exhausted_budget_hands_off_and_alerts_owner_once(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    _server, settings = await start_bot(llm_daily_token_budget=1000)
    await db_admin.execute(
        f"insert into {S}.llm_calls (purpose, model, prompt_tokens, completion_tokens, latency_ms) "
        "values ('answer', 'test', 900, 200, 10)"
    )

    state.push(message_update(1, user_id=CLIENT_ID, text="сколько стоит?"))
    await settle(state)

    assert state.texts_to(CLIENT_ID) == [texts.client_handoff(settings)]
    assert llm_state.chat_requests == []  # модель не вызывалась
    owner = state.texts_to(OWNER_ID)
    assert any("бюджет модели исчерпан" in t for t in owner)
    assert any("Нужен ваш ответ: дневной бюджет модели исчерпан" in t for t in owner)
    assert await db_admin.fetchval(f"select handoff_reason from {S}.conversations") == "budget"

    # Второе сообщение: клиенту тот же ответ, владельцу без второго алерта.
    alerts_before = sum("бюджет модели исчерпан:" in t for t in state.texts_to(OWNER_ID))
    state.push(message_update(2, user_id=CLIENT_ID, text="а всё-таки?"))
    await settle(state)
    assert sum("бюджет модели исчерпан:" in t for t in state.texts_to(OWNER_ID)) == alerts_before
    assert await db_admin.fetchval(f"select count(*) from {S}.events where type = 'budget_exhausted'") == 1


# --- кабинет -------------------------------------------------------------------


@pytest.fixture
async def cabinet(start_bot):
    server, settings = await start_bot(
        cabinet_login="owner",
        cabinet_password_hash=hash_password(PASSWORD),
        cabinet_cookie_secure=False,
        cabinet_login_attempts=3,
        cabinet_login_block_minutes=10,
        api_max_body_bytes=2048,
    )
    async with httpx.AsyncClient(base_url=server.base_url, timeout=10) as client:
        yield client, settings


async def login(client, password=PASSWORD):
    return await client.post("/api/login", json={"login": "owner", "password": password})


async def test_login_is_blocked_after_failed_attempts(cabinet):
    client, _settings = cabinet

    for _ in range(3):
        assert (await login(client, "неверный")).status_code == 401

    blocked = await login(client)  # даже верный пароль
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_cross_site_mutation_is_rejected(cabinet, fake_tg, llm):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())
    await login(client)
    state.push(message_update(1, user_id=CLIENT_ID, text="позовите человека"))
    await settle(state)
    conversation_id = (await client.get("/api/conversations")).json()["items"][0]["id"]

    evil = await client.post(
        f"/api/conversations/{conversation_id}/reply",
        json={"text": "чужой сайт"},
        headers={"Origin": "https://evil.example"},
    )
    assert evil.status_code == 403
    fetch_site = await client.post(
        f"/api/conversations/{conversation_id}/reply",
        json={"text": "чужой сайт"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert fetch_site.status_code == 403
    assert state.texts_to(CLIENT_ID)[-1] != "чужой сайт"

    own = await client.post(
        f"/api/conversations/{conversation_id}/reply",
        json={"text": "свой сайт"},
        headers={"Origin": f"http://127.0.0.1:3000"},
    )
    assert own.status_code == 200


async def test_oversized_body_is_rejected_before_parsing(cabinet):
    client, _settings = cabinet

    response = await client.post(
        "/api/login",
        content=b'{"login":"owner","password":"' + b"x" * 5000 + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


async def test_ban_from_cabinet(cabinet, fake_tg, llm, db_admin):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(plain_reply("Ответ"))
    await login(client)
    state.push(message_update(1, user_id=CLIENT_ID, text="привет"))
    await settle(state)
    client_id = (await client.get("/api/conversations")).json()["items"][0]["client"]["id"]

    assert (await client.patch(f"/api/clients/{client_id}", json={"is_banned": True})).status_code == 200
    assert await db_admin.fetchval(f"select is_banned from {S}.clients") is True
    assert (await client.patch("/api/clients/999999", json={"is_banned": True})).status_code == 404


# --- логи и база ---------------------------------------------------------------


def test_tracebacks_do_not_leak_secrets():
    secret = "123456:ABC-DEF-secret-token"
    redactor = SecretRedactor([secret])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(redactor))
    handler.addFilter(redactor)
    logger = logging.getLogger("test.secrets")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise RuntimeError(f"connect failed: https://api.telegram.org/bot{secret}/getUpdates")
        except RuntimeError:
            logger.exception("упало", extra={"url": f"bot{secret}"})
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert secret not in output
    assert "***" in output


async def test_bot_role_cannot_rewrite_migration_history(database, db_admin):
    conn = await asyncpg.connect(database.bot_url)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(f"delete from {S}.schema_migrations")
    finally:
        await conn.close()
