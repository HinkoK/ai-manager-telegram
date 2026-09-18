"""Этап 5: квалификация и заявки.

Модель говорит, что клиент попросил записать его, и что он при этом назвал.
Всё остальное решает код: какие поля принять, хватает ли их и когда написать
владельцу. Модель объявляет заявку готовой слишком охотно, поэтому ей это
решение не доверено.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.leads import clean_lead_patch, is_qualified, owner_card
from tests.conftest import CLIENT_ID, OWNER_ID, TEST_SCHEMA, wait_until
from tests.fake_llm import EMPTY_PROFILE_UPDATES
from tests.fake_telegram import message_update

S = TEST_SCHEMA

# Карточку собирает чистая функция: ни база, ни сеть ей не нужны.
CARD_SETTINGS = Settings(
    _env_file=None, telegram_bot_token="test-token-0123456789", owner_telegram_id=1,
    postgres_url="postgresql://user:pass@127.0.0.1:5432/db", llm_api_key="test-key-0123456789",
    llm_model="test/chat-model", embedding_model="test/embedding-model", embedding_dim=8,
)


def reply(text: str, lead: dict | None = None) -> dict:
    return {
        "sources": [], "needs_human": False, "handoff_reason": None, "is_smalltalk": True,
        "reply": text, "profile_updates": EMPTY_PROFILE_UPDATES, "lead": lead,
    }


def lead_of(kind: str, **fields) -> dict:
    empty = {
        "goal": None, "level": None, "format": None, "timezone": None, "preferred_time": None,
        "company": None, "team_size": None, "sphere": None,
    }
    return {"kind": kind, **empty, **fields}


async def ask(state, text: str, update_id: int, *, expect_client: int) -> None:
    state.push(message_update(update_id, user_id=CLIENT_ID, text=text))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == expect_client)
    assert await wait_until(lambda: state.pending == [])


def test_lead_patch_is_whitelisted_by_kind():
    raw = lead_of("trial", goal=" работа ", team_size=5, sphere="IT", preferred_time="вечера")

    patch = clean_lead_patch("trial", raw, known={})

    assert patch == {"goal": "работа", "preferred_time": "вечера"}  # корпоративных полей у пробного нет
    assert clean_lead_patch("corporate", lead_of("corporate", team_size="5"), {}) == {"team_size": 5}
    assert clean_lead_patch("corporate", lead_of("corporate", team_size=0), {}) == {}


def test_time_without_timezone_is_not_enough():
    assert not is_qualified("trial", {"goal": "работа", "preferred_time": "вечера"})
    assert is_qualified("trial", {"goal": "работа", "preferred_time": "вечера", "timezone": "МСК"})
    assert not is_qualified("corporate", {"goal": "созвоны", "team_size": 5, "preferred_time": "утро"})


def test_owner_card_shows_what_is_missing():
    card = owner_card(
        CARD_SETTINGS,
        client={"first_name": "Марина", "username": "marina_k"},
        kind="trial",
        lead={"goal": "переезд", "preferred_time": None, "timezone": None},
        conversation_id=7,
        qualified=False,
    )
    assert "Новая заявка: пробный урок" in card
    assert "Марина (@marina_k)" in card
    assert "цель: переезд" in card
    assert "Не хватает: удобное время" in card
    assert "Разговор №7" in card


async def test_price_question_creates_no_lead(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(reply("Индивидуальное занятие стоит $18."))

    await ask(state, "сколько стоит занятие?", 1, expect_client=1)

    assert await db_admin.fetchval(f"select count(*) from {S}.leads") == 0
    assert state.texts_to(OWNER_ID) == []


async def test_trial_lead_collected_step_by_step(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [
        reply("Отлично! В каком часовом поясе вам удобно?", lead_of("trial", goal="работа")),
        reply("Записал, менеджер предложит окна.",
              lead_of("trial", preferred_time="будни после 19:00", timezone="Алматы")),
        reply("Хорошо, учту уровень.", lead_of("trial", level="B1")),
    ]

    # 1. Заявка появилась, но данных не хватает: владельцу уходит короткое уведомление.
    await ask(state, "хочу пробный урок, английский нужен для работы", 1, expect_client=1)
    lead = await db_admin.fetchrow(f"select * from {S}.leads")
    assert (lead["kind"], lead["goal"], lead["is_qualified"]) == ("trial", "работа", False)
    assert lead["notified_at"] is not None and lead["qualified_notified_at"] is None
    owner = state.texts_to(OWNER_ID)
    assert len(owner) == 1
    assert "Новая заявка: пробный урок" in owner[0] and "Не хватает: удобное время" in owner[0]

    # 2. Данных стало достаточно: второе и последнее уведомление.
    await ask(state, "будни после 19:00, я в Алматы", 2, expect_client=2)
    lead = await db_admin.fetchrow(f"select * from {S}.leads")
    assert lead["is_qualified"] is True and lead["qualified_notified_at"] is not None
    owner = state.texts_to(OWNER_ID)
    assert len(owner) == 2
    assert "Заявка собрана: пробный урок" in owner[1]
    assert "удобное время: будни после 19:00" in owner[1] and "часовой пояс: Алматы" in owner[1]

    # 3. Уточнение заявку дополняет, но владельца не беспокоит.
    await ask(state, "уровень примерно B1", 3, expect_client=3)
    assert await db_admin.fetchval(f"select level from {S}.leads") == "B1"
    assert len(state.texts_to(OWNER_ID)) == 2
    assert await db_admin.fetchval(f"select count(*) from {S}.leads") == 1

    types = [r["type"] for r in await db_admin.fetch(f"select type from {S}.events order by id")]
    assert types.count("lead_created") == 1 and types.count("lead_updated") == 2
    assert types.count("owner_notified") == 2


async def test_corporate_lead_keeps_team_fields(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies.append(reply(
        "Передал заявку менеджеру.",
        lead_of("corporate", team_size=5, sphere="IT", goal="созвоны с коллегами",
                preferred_time="по вечерам", timezone="МСК", company="Acme"),
    ))

    await ask(state, "у нас команда 5 человек, IT, нужен английский для созвонов, по вечерам", 1, expect_client=1)

    lead = await db_admin.fetchrow(f"select * from {S}.leads")
    assert (lead["kind"], lead["team_size"], lead["sphere"]) == ("corporate", 5, "IT")
    assert lead["is_qualified"] is True
    owner = state.texts_to(OWNER_ID)
    # Заявка пришла сразу полной: одно сообщение, а не два.
    assert len(owner) == 1
    assert "Заявка собрана: корпоративное обучение" in owner[0]
    assert "человек в команде: 5" in owner[0] and "сфера: IT" in owner[0]
    assert lead["notified_at"] is not None and lead["qualified_notified_at"] is not None


async def test_second_request_updates_the_same_lead(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [
        reply("Записал.", lead_of("trial", goal="работа")),
        reply("Уточнил.", lead_of("trial", goal="переезд")),
    ]

    await ask(state, "хочу пробный", 1, expect_client=1)
    await ask(state, "вообще-то цель другая: переезд", 2, expect_client=2)

    leads = await db_admin.fetch(f"select id, goal from {S}.leads")
    assert len(leads) == 1 and leads[0]["goal"] == "переезд"


async def test_undelivered_notification_is_retried(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    state.forbidden_chats.add(OWNER_ID)  # владелец заблокировал бота
    llm_state.replies += [
        reply("Записал.", lead_of("trial", goal="работа")),
        reply("Уточнил.", lead_of("trial", preferred_time="вечера", timezone="МСК")),
    ]

    await ask(state, "хочу пробный, для работы", 1, expect_client=1)
    assert state.texts_to(OWNER_ID) == []
    assert await db_admin.fetchval(f"select notified_at from {S}.leads") is None

    state.forbidden_chats.clear()
    await ask(state, "вечера по Москве", 2, expect_client=2)

    owner = state.texts_to(OWNER_ID)
    assert len(owner) == 1  # то самое уведомление, а не потерянное
    assert await db_admin.fetchval(f"select notified_at from {S}.leads") is not None


async def test_lead_card_link_points_to_cabinet(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot(cabinet_url="https://school.example/cabinet/")
    llm_state.replies.append(reply("Записал.", lead_of("trial", goal="работа")))

    await ask(state, "хочу пробный урок для работы", 1, expect_client=1)

    conversation_id = await db_admin.fetchval(f"select id from {S}.conversations")
    assert f"https://school.example/cabinet/conversations/{conversation_id}" in state.texts_to(OWNER_ID)[0]
