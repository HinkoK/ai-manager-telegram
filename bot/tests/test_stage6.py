"""Этап 6: передача разговора владельцу.

Здесь два собеседника в одном чате бота: клиент и владелец. Проверяем переходы
состояний, ретрансляцию, доставку ответа владельца, возврат боту и фоновую
задачу, которая торопит и возвращает разговоры. Время двигаем через базу.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import texts
from app.db import Database
from app.jobs import Jobs
from app.repo import Repo
from tests.conftest import CLIENT_ID, OWNER_ID, TEST_SCHEMA, wait_until
from tests.fake_llm import EMPTY_PROFILE_UPDATES
from tests.fake_telegram import message_update

S = TEST_SCHEMA
OTHER_CLIENT_ID = 777_000_333


def handoff_reply(reason: str = "not_in_knowledge") -> dict:
    return {
        "sources": [], "needs_human": True, "handoff_reason": reason, "is_smalltalk": False,
        "reply": "", "profile_updates": EMPTY_PROFILE_UPDATES, "lead": None,
    }


def plain_reply(text: str = "Ответ бота") -> dict:
    return {
        "sources": [], "needs_human": False, "handoff_reason": None, "is_smalltalk": True,
        "reply": text, "profile_updates": EMPTY_PROFILE_UPDATES, "lead": None,
    }


async def client_says(state, text: str, update_id: int, *, expect: int, user_id: int = CLIENT_ID) -> None:
    state.push(message_update(update_id, user_id=user_id, text=text))
    assert await wait_until(lambda: len(state.texts_to(user_id)) == expect)
    assert await wait_until(lambda: state.pending == [])


async def owner_says(state, text: str, update_id: int, *, reply_to: int | None = None, expect: int) -> None:
    extra = {"reply_to_message": {"message_id": reply_to}} if reply_to is not None else None
    state.push(message_update(update_id, user_id=OWNER_ID, text=text, extra=extra))
    assert await wait_until(lambda: len(state.texts_to(OWNER_ID)) == expect)
    assert await wait_until(lambda: state.pending == [])


@pytest.fixture
async def jobs(database, db_admin, fake_tg, start_bot):
    """Фоновая задача отдельно от бота: витки дёргаем руками, а не ждём минуту."""
    _state, tg_server = fake_tg
    server, settings = await start_bot()
    db = Database(database.bot_url, database.schema, min_size=1, max_size=2)
    await db.connect()
    from app.telegram import TelegramClient

    tg = TelegramClient(settings.telegram_bot_token, tg_server.base_url, 1)
    try:
        yield Jobs(settings, Repo(db), tg), settings
    finally:
        await tg.aclose()
        await db.close()


async def test_model_asks_for_human_and_owner_gets_card(bot, llm, db_admin):
    state, _server, settings = bot
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())

    await client_says(state, "Можно оплатить картой Kaspi?", 1, expect=1)

    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    card = state.texts_to(OWNER_ID)[0]
    assert "Нужен ваш ответ: нет ответа в базе знаний" in card
    assert "Можно оплатить картой Kaspi?" in card  # последние реплики
    assert "Ответьте реплаем" in card

    conversation = await db_admin.fetchrow(f"select state, handoff_reason, handoff_at from {S}.conversations")
    assert conversation["state"] == "HUMAN_REQUESTED"
    assert conversation["handoff_reason"] == "not_in_knowledge"
    assert conversation["handoff_at"] is not None
    event = await db_admin.fetchrow(f"select payload from {S}.events where type = 'handoff_started'")
    assert json.loads(event["payload"]) == {"reason": "not_in_knowledge"}


async def test_human_command_hands_off_without_model(bot, llm, db_admin):
    state, _server, settings = bot
    llm_state, _ = llm

    await client_says(state, "/human", 1, expect=1)

    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    assert llm_state.chat_requests == []  # модель не вызывалась
    assert await db_admin.fetchval(f"select handoff_reason from {S}.conversations") == "command"
    assert "клиент написал /human" in state.texts_to(OWNER_ID)[0]


async def test_client_messages_are_relayed_and_owner_reply_delivered(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply("client_request"))

    llm_state.replies.append(plain_reply("Пробный урок бесплатный"))
    await client_says(state, "позовите человека", 1, expect=1)
    card_message_id = state.sent[-1]["message_id"]  # уведомление владельцу

    # Владелец ещё не подключился: на новый вопрос бот отвечает сам, а владелец
    # видит и вопрос, и ответ.
    await client_says(state, "а пробный урок платный?", 2, expect=2)
    assert state.texts_to(CLIENT_ID)[-1] == "Пробный урок бесплатный"
    relay = state.texts_to(OWNER_ID)[-1]
    assert "а пробный урок платный?" in relay and "Бот ответил сам: Пробный урок бесплатный" in relay
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "HUMAN_REQUESTED"

    await owner_says(state, "Здравствуйте, это владелец школы. Отвечаю вам.", 3,
                     reply_to=card_message_id, expect=3)

    assert state.texts_to(CLIENT_ID)[-1] == "Здравствуйте, это владелец школы. Отвечаю вам."
    assert "Отправил клиенту" in state.texts_to(OWNER_ID)[-1]
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "HUMAN_ACTIVE"
    roles = [r["role"] for r in await db_admin.fetch(f"select role from {S}.messages order by id")]
    assert roles == ["client", "bot", "client", "bot", "owner"]

    # Владелец заговорил: теперь бот молчит, чтобы не говорить поверх него.
    await client_says(state, "и ещё вопрос", 4, expect=3)
    assert len(state.texts_to(CLIENT_ID)) == 3
    assert await db_admin.fetchval(f"select count(*) from {S}.events where type = 'owner_reply'") == 1


async def test_owner_reply_without_reply_to_goes_to_the_only_conversation(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())

    await client_says(state, "вопрос не из базы", 1, expect=1)
    await owner_says(state, "Отвечаю без реплая", 2, expect=2)

    assert state.texts_to(CLIENT_ID)[-1] == "Отвечаю без реплая"


async def test_owner_reply_without_reply_to_is_refused_when_two_conversations(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [handoff_reply(), handoff_reply()]

    await client_says(state, "первый клиент", 1, expect=1)
    await client_says(state, "второй клиент", 2, expect=1, user_id=OTHER_CLIENT_ID)
    await owner_says(state, "кому-то из вас", 3, expect=3)

    assert "несколько разговоров" in state.texts_to(OWNER_ID)[-1]
    # Клиентам ничего не ушло, кроме их собственных ответов о передаче.
    assert len(state.texts_to(CLIENT_ID)) == 1 and len(state.texts_to(OTHER_CLIENT_ID)) == 1


async def test_bot_command_returns_conversation(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [handoff_reply(), plain_reply("Снова отвечаю я")]

    await client_says(state, "позовите человека", 1, expect=1)
    await owner_says(state, "/bot", 2, expect=2)

    assert "снова у бота" in state.texts_to(OWNER_ID)[-1]
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "AI_ACTIVE"
    assert await db_admin.fetchval(f"select returned_at is not null from {S}.conversations")
    event = await db_admin.fetchrow(f"select payload from {S}.events where type = 'handoff_returned'")
    assert json.loads(event["payload"]) == {"by": "owner"}

    await client_says(state, "а сколько стоит?", 3, expect=2)
    assert state.texts_to(CLIENT_ID)[-1] == "Снова отвечаю я"


async def test_status_shows_counters(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())

    await client_says(state, "вопрос не из базы", 1, expect=1)
    await owner_says(state, "/status", 2, expect=2)

    status = state.texts_to(OWNER_ID)[-1]
    assert "Разговоров у вас: 1" in status
    assert "ждут первого ответа: 1" in status
    assert "Новых заявок: 0" in status


async def test_reminder_goes_once_after_two_hours(jobs, fake_tg, llm, db_admin):
    job, settings = jobs
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())

    await client_says(state, "вопрос не из базы", 1, expect=1)
    owner_before = len(state.texts_to(OWNER_ID))

    assert (await job.tick()).reminded == 0  # свежий разговор не торопим

    await db_admin.execute(
        f"update {S}.conversations set handoff_at = now() - make_interval(hours => $1::int)",
        int(settings.handoff_reminder_hours) + 1,
    )
    assert (await job.tick()).reminded == 1
    assert "Клиент ждёт ответа больше" in state.texts_to(OWNER_ID)[-1]
    assert await db_admin.fetchval(f"select reminded_at is not null from {S}.conversations")

    # Второй виток молчит: напоминание одно.
    assert (await job.tick()).reminded == 0
    assert len(state.texts_to(OWNER_ID)) == owner_before + 1


async def test_conversation_returns_to_bot_after_a_day(jobs, fake_tg, llm, db_admin):
    job, settings = jobs
    state, _tg = fake_tg
    llm_state, _ = llm
    llm_state.replies += [handoff_reply(), plain_reply("Отвечаю снова")]

    await client_says(state, "вопрос не из базы", 1, expect=1)
    client_before = len(state.texts_to(CLIENT_ID))

    await db_admin.execute(
        f"update {S}.conversations set handoff_at = now() - make_interval(hours => $1::int)",
        int(settings.handoff_return_hours) + 1,
    )
    assert (await job.tick()).returned == 1

    conversation = await db_admin.fetchrow(f"select state, needs_attention from {S}.conversations")
    assert conversation["state"] == "AI_ACTIVE" and conversation["needs_attention"] is True
    assert "вернулся боту" in state.texts_to(OWNER_ID)[-1]
    # Клиенту про возврат не пишем.
    assert len(state.texts_to(CLIENT_ID)) == client_before

    await client_says(state, "я всё ещё тут", 2, expect=client_before + 1)
    assert state.texts_to(CLIENT_ID)[-1] == "Отвечаю снова"
    event = await db_admin.fetchrow(f"select payload from {S}.events where type = 'handoff_returned'")
    assert json.loads(event["payload"])["by"] == "timeout"


async def test_owner_reply_to_blocked_client_is_reported(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(handoff_reply())

    await client_says(state, "вопрос не из базы", 1, expect=1)
    state.forbidden_chats.add(CLIENT_ID)
    await owner_says(state, "Отвечаю вам", 2, expect=2)

    assert "заблокировал бота" in state.texts_to(OWNER_ID)[-1]
    roles = [r["role"] for r in await db_admin.fetch(f"select role from {S}.messages order by id")]
    assert "owner" not in roles  # недоставленное сообщение в историю не пишем


async def test_second_trigger_while_waiting_does_not_send_second_card(bot, llm, db_admin):
    """Переданный вопрос остаётся у владельца, второй карточки на него нет."""
    state, _server, settings = bot
    llm_state, _ = llm
    llm_state.replies += [handoff_reply("not_in_knowledge"), handoff_reply("complaint")]

    await client_says(state, "вопрос не из базы", 1, expect=1)
    cards = len(state.texts_to(OWNER_ID))

    await client_says(state, "и ещё один вопрос не из базы", 2, expect=2)

    assert state.texts_to(CLIENT_ID)[-1] == texts.client_handoff(settings)
    # Владелец получил ретрансляцию, а не вторую карточку передачи.
    assert len(state.texts_to(OWNER_ID)) == cards + 1
    assert "Нужен ваш ответ" not in state.texts_to(OWNER_ID)[-1]
    conversation = await db_admin.fetchrow(f"select state, handoff_reason from {S}.conversations")
    assert conversation["state"] == "HUMAN_REQUESTED"
    assert conversation["handoff_reason"] == "not_in_knowledge"  # причина первой передачи
    types = [r["type"] for r in await db_admin.fetch(f"select type from {S}.events order by id")]
    assert types.count("handoff_started") == 1 and types.count("handoff_needed") == 1
