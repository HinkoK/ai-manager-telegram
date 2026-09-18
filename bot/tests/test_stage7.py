"""Этап 7: REST API кабинета.

Кабинет ходит только сюда. Тесты бьют по настоящим маршрутам настоящего
приложения httpx-клиентом с cookie, как браузер. Ответ клиенту уходит в
фейковый Telegram, как и у ветки владельца.
"""

from __future__ import annotations

import httpx
import pytest

from app.auth import hash_password, verify_password
from tests.conftest import CLIENT_ID, OWNER_ID, TEST_SCHEMA, wait_until
from tests.fake_llm import EMPTY_PROFILE_UPDATES
from tests.fake_telegram import message_update

S = TEST_SCHEMA
PASSWORD = "cabinet-password-123"


def handoff_reply() -> dict:
    return {
        "sources": [], "needs_human": True, "handoff_reason": "not_in_knowledge",
        "is_smalltalk": False, "reply": "", "profile_updates": EMPTY_PROFILE_UPDATES, "lead": None,
    }


def lead_reply(text: str = "Записал заявку") -> dict:
    return {
        "sources": [], "needs_human": False, "handoff_reason": None, "is_smalltalk": True,
        "reply": text, "profile_updates": EMPTY_PROFILE_UPDATES,
        "lead": {"kind": "trial", "goal": "работа", "level": None, "format": None,
                 "timezone": "МСК", "preferred_time": "вечера", "company": None,
                 "team_size": None, "sphere": None},
    }


@pytest.fixture
async def cabinet(start_bot):
    """Бот с настроенным кабинетом и браузерный клиент с cookie."""
    server, settings = await start_bot(
        cabinet_login="owner",
        cabinet_password_hash=hash_password(PASSWORD),
        cabinet_cookie_secure=False,
    )
    async with httpx.AsyncClient(base_url=server.base_url, timeout=10) as client:
        yield client, settings


async def login(client: httpx.AsyncClient, password: str = PASSWORD, login: str = "owner"):
    return await client.post("/api/login", json={"login": login, "password": password})


def test_password_hash_is_not_the_password():
    stored = hash_password(PASSWORD)

    assert PASSWORD not in stored
    assert stored.startswith("scrypt:")
    # Доллара в хеше быть не должно: docker compose съедает его как подстановку
    # переменной, и в контейнер приходит обрезанный хеш.
    assert "$" not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("не тот пароль", stored)
    # Две записи одного пароля отличаются: соль случайная.
    assert hash_password(PASSWORD) != stored


async def test_api_requires_session(cabinet):
    client, _settings = cabinet

    for method, path in [("get", "/api/me"), ("get", "/api/conversations"), ("get", "/api/leads")]:
        response = await getattr(client, method)(path)
        assert response.status_code == 401, path


async def test_login_sets_cookie_and_logout_revokes_it(cabinet, db_admin):
    client, _settings = cabinet

    bad = await login(client, password="неверный")
    assert bad.status_code == 401
    assert (await client.get("/api/me")).status_code == 401

    good = await login(client)
    assert good.status_code == 200 and good.json()["login"] == "owner"
    cookie = good.cookies.get("school_session")
    assert cookie and "httponly" in good.headers["set-cookie"].lower()
    # В базе только хеш токена.
    stored = await db_admin.fetchval(f"select token_hash from {S}.owner_sessions")
    assert stored and cookie not in stored

    assert (await client.get("/api/me")).status_code == 200
    assert (await client.post("/api/logout")).status_code == 200
    assert (await client.get("/api/me")).status_code == 401


async def test_expired_session_is_rejected(cabinet, db_admin):
    client, _settings = cabinet
    await login(client)

    await db_admin.execute(f"update {S}.owner_sessions set expires_at = now() - interval '1 day'")

    assert (await client.get("/api/me")).status_code == 401


async def test_conversations_list_filters_and_unread(cabinet, fake_tg, llm, db_admin):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())
    await login(client)

    state.push(message_update(1, user_id=CLIENT_ID, text="вопрос не из базы"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await wait_until(lambda: state.pending == [])

    all_items = (await client.get("/api/conversations")).json()["items"]
    assert len(all_items) == 1
    item = all_items[0]
    assert item["state"] == "HUMAN_REQUESTED"
    assert item["unread"] is True
    assert item["client"]["telegram_user_id"] == CLIENT_ID
    assert item["last_text"]

    human = (await client.get("/api/conversations", params={"filter": "human"})).json()["items"]
    bot_only = (await client.get("/api/conversations", params={"filter": "bot"})).json()["items"]
    assert len(human) == 1 and bot_only == []
    assert (await client.get("/api/conversations", params={"filter": "чушь"})).status_code == 400

    detail = (await client.get(f"/api/conversations/{item['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["client", "bot"]
    assert detail["handoff_reason"] == "not_in_knowledge"
    # Событие передачи видно в переписке отдельной строкой.
    assert [e["type"] for e in detail["events"]] == ["handoff_started"]
    assert "нет ответа в базе знаний" in detail["events"][0]["title"]

    # Само чтение разговора пометок не снимает: опрос списка не должен их гасить.
    assert (await client.get("/api/conversations")).json()["items"][0]["unread"] is True
    assert (await client.post(f"/api/conversations/{item['id']}/seen")).status_code == 200
    after = (await client.get("/api/conversations")).json()["items"][0]
    assert after["unread"] is False and after["needs_attention"] is False


async def test_reply_from_cabinet_reaches_client(cabinet, fake_tg, llm, db_admin):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())
    await login(client)

    state.push(message_update(1, user_id=CLIENT_ID, text="вопрос не из базы"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await wait_until(lambda: state.pending == [])
    conversation_id = (await client.get("/api/conversations")).json()["items"][0]["id"]

    response = await client.post(
        f"/api/conversations/{conversation_id}/reply", json={"text": "Отвечаю из кабинета"}
    )

    assert response.status_code == 200
    assert state.texts_to(CLIENT_ID)[-1] == "Отвечаю из кабинета"
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "HUMAN_ACTIVE"
    roles = [r["role"] for r in await db_admin.fetch(f"select role from {S}.messages order by id")]
    assert roles == ["client", "bot", "owner"]

    returned = await client.post(f"/api/conversations/{conversation_id}/return")
    assert returned.json()["changed"] is True
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "AI_ACTIVE"


async def test_reply_to_blocked_client_is_conflict(cabinet, fake_tg, llm):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())
    await login(client)

    state.push(message_update(1, user_id=CLIENT_ID, text="вопрос не из базы"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await wait_until(lambda: state.pending == [])
    conversation_id = (await client.get("/api/conversations")).json()["items"][0]["id"]
    state.forbidden_chats.add(CLIENT_ID)

    response = await client.post(
        f"/api/conversations/{conversation_id}/reply", json={"text": "Не дойдёт"}
    )

    assert response.status_code == 409


async def test_leads_list_and_status_change(cabinet, fake_tg, llm, db_admin):
    client, _settings = cabinet
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(lead_reply())
    await login(client)

    state.push(message_update(1, user_id=CLIENT_ID, text="хочу пробный, для работы, вечера по мск"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await wait_until(lambda: state.pending == [])

    items = (await client.get("/api/leads")).json()["items"]
    assert len(items) == 1
    lead = items[0]
    assert lead["kind"] == "trial" and lead["status"] == "new"
    assert lead["fields"]["goal"] == "работа" and lead["is_qualified"] is True

    patched = await client.patch(
        f"/api/leads/{lead['id']}", json={"status": "in_progress", "notes": "позвонить завтра"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_progress"
    assert patched.json()["fields"]["notes"] == "позвонить завтра"

    assert (await client.get("/api/leads", params={"status": "new"})).json()["items"] == []
    assert len((await client.get("/api/leads", params={"status": "in_progress"})).json()["items"]) == 1
    assert (await client.patch(f"/api/leads/{lead['id']}", json={})).status_code == 400
    assert (await client.patch("/api/leads/999999", json={"status": "done"})).status_code == 404

    # Разговор показывает ту же заявку.
    conversation_id = (await client.get("/api/conversations")).json()["items"][0]["id"]
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert [l["id"] for l in detail["leads"]] == [lead["id"]]


async def test_cabinet_without_password_is_disabled(start_bot):
    server, _settings = await start_bot(cabinet_password_hash=None)

    async with httpx.AsyncClient(base_url=server.base_url, timeout=10) as client:
        response = await client.post("/api/login", json={"login": "owner", "password": "любой"})

    assert response.status_code == 503
