"""Этап 4: память.

Модель по-прежнему ничего не помнит сама: всё, что она знает о клиенте, бот
кладёт в запрос. Здесь проверяется, что кладёт именно то и ровно столько.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.memory import clean_profile_patch
from tests.conftest import CLIENT_ID, TEST_SCHEMA, wait_until
from tests.fake_llm import EMPTY_PROFILE_UPDATES
from tests.fake_telegram import message_update

S = TEST_SCHEMA


def reply(text: str, **profile: object) -> dict:
    """Ответ модели с необязательными фактами о клиенте."""
    return {
        "sources": [], "needs_human": False, "handoff_reason": None, "is_smalltalk": True,
        "reply": text, "profile_updates": {**EMPTY_PROFILE_UPDATES, **profile},
    }


async def ask(state, text: str, update_id: int, *, expect: int) -> None:
    """Отправляет сообщение и ждёт, пока бот ответит и допишет базу."""
    state.push(message_update(update_id, user_id=CLIENT_ID, text=text))
    assert await wait_until(lambda: len(state.sent) == expect)
    assert await wait_until(lambda: state.pending == [])


def test_profile_patch_keeps_only_known_and_new_fields():
    raw = {
        "name": " Анна ", "goal": "переезд", "level": None, "timezone": "",
        "preferred_time": "вечера", "format_interest": None, "is_teen": False,
        "secret_field": "игнорировать",
    }
    patch = clean_profile_patch(raw, known={"preferred_time": "вечера"})

    assert patch == {"name": "Анна", "goal": "переезд", "is_teen": False}
    assert "secret_field" not in patch


async def test_episode_history_goes_to_model(bot, llm):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [reply("Первый ответ"), reply("Второй ответ"), reply("Третий ответ")]

    await ask(state, "первый вопрос", 1, expect=1)
    await ask(state, "второй вопрос", 2, expect=2)
    await ask(state, "третий вопрос", 3, expect=3)

    assert llm_state.history() == [
        ("user", "первый вопрос"), ("assistant", "Первый ответ"),
        ("user", "второй вопрос"), ("assistant", "Второй ответ"),
    ]
    assert llm_state.user_message() == "третий вопрос"


async def test_history_is_cut_to_the_limit(fake_tg, llm, start_bot):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot(history_messages=2)
    llm_state.replies += [reply("Ответ 1"), reply("Ответ 2"), reply("Ответ 3")]

    await ask(state, "вопрос 1", 1, expect=1)
    await ask(state, "вопрос 2", 2, expect=2)
    await ask(state, "вопрос 3", 3, expect=3)

    assert llm_state.history() == [("user", "вопрос 2"), ("assistant", "Ответ 2")]


async def test_new_episode_gets_summary_instead_of_history(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot()
    llm_state.replies += [reply("Ответ про IELTS"), reply("Ответ во втором эпизоде")]
    llm_state.summaries.append({"summary": "Клиент готовится к IELTS, уровень B1."})

    await ask(state, "готовлюсь к IELTS", 1, expect=1)
    # Сутки тишины: следующий вопрос откроет новый эпизод.
    await db_admin.execute(f"update {S}.conversations set last_message_at = now() - interval '25 hours'")
    await ask(state, "какой курс подойдёт?", 2, expect=2)

    assert llm_state.history() == []  # прошлый эпизод историей не приходит
    prompt = llm_state.system_prompt()
    assert "Клиент готовится к IELTS, уровень B1." in prompt

    summary, summary_message_id = await db_admin.fetchrow(
        f"select summary, summary_message_id from {S}.clients"
    )
    assert summary == "Клиент готовится к IELTS, уровень B1."
    assert summary_message_id == 2  # резюме доведено до последнего сообщения прошлого эпизода
    assert len(llm_state.requests_for("client_summary")) == 1


async def test_summary_is_rebuilt_every_n_messages(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot(summary_every_messages=4)
    llm_state.replies += [reply("Ответ 1"), reply("Ответ 2"), reply("Ответ 3")]

    await ask(state, "вопрос 1", 1, expect=1)
    assert llm_state.requests_for("client_summary") == []  # накопилось 2 сообщения из 4

    await ask(state, "вопрос 2", 2, expect=2)
    assert await wait_until(lambda: len(llm_state.requests_for("client_summary")) == 1)
    assert await db_admin.fetchval(f"select summary from {S}.clients") == "Фейковое резюме клиента"

    await ask(state, "вопрос 3", 3, expect=3)
    await asyncio.sleep(0.3)
    assert len(llm_state.requests_for("client_summary")) == 1  # после каждого ответа не считаем


async def test_profile_is_filled_and_used_in_next_answer(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [
        reply("Приятно познакомиться!", name="Анна", goal="переезд в Канаду"),
        reply("Вот подходящий курс"),
    ]

    await ask(state, "меня зовут Анна, переезжаю в Канаду", 1, expect=1)
    profile = json.loads(await db_admin.fetchval(f"select profile from {S}.clients"))
    assert profile == {"name": "Анна", "goal": "переезд в Канаду"}

    await ask(state, "какой курс мне подойдёт?", 2, expect=2)
    prompt = llm_state.system_prompt()
    assert "переезд в Канаду" in prompt and "Анна" in prompt


async def test_search_uses_rewritten_question(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [reply("Цены на IELTS"), reply("Цена пакета")]
    llm_state.rewrites.append({"query": "стоимость подготовки к IELTS пакет 24 занятия"})

    await ask(state, "сколько стоит IELTS?", 1, expect=1)
    # Первое сообщение эпизода переписывать не из чего.
    assert llm_state.requests_for("search_query") == []
    assert llm_state.embed_requests[-1]["input"] == ["сколько стоит IELTS?"]

    await ask(state, "а если 24 занятия?", 2, expect=2)
    assert len(llm_state.requests_for("search_query")) == 1
    assert llm_state.embed_requests[-1]["input"] == ["стоимость подготовки к IELTS пакет 24 занятия"]
    meta = json.loads(await db_admin.fetchval(
        f"select meta from {S}.messages where role = 'bot' order by id desc limit 1"
    ))
    assert meta["search_query"] == "стоимость подготовки к IELTS пакет 24 занятия"
    assert meta["history_messages"] == 2


async def test_rewrite_failure_falls_back_to_raw_question(bot, llm, db_admin):
    state, _server, _settings = bot
    llm_state, _ = llm
    llm_state.replies += [reply("Ответ 1"), reply("Ответ 2")]

    await ask(state, "сколько стоит IELTS?", 1, expect=1)
    llm_state.failing_schema, llm_state.chat_status = "search_query", 500
    await ask(state, "а если 24 занятия?", 2, expect=2)

    assert state.sent[-1]["text"] == "Ответ 2"  # клиент ответ всё равно получил
    assert llm_state.embed_requests[-1]["input"] == ["а если 24 занятия?"]
    assert await db_admin.fetchval(
        f"select error from {S}.llm_calls where purpose = 'rewrite' order by id desc limit 1"
    ) == "http_500"


async def test_summary_failure_keeps_answer_and_old_summary(fake_tg, llm, start_bot, db_admin):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot(summary_every_messages=2)
    llm_state.replies += [reply("Ответ 1"), reply("Ответ 2")]
    llm_state.failing_schema, llm_state.chat_status = "client_summary", 500

    await ask(state, "вопрос 1", 1, expect=1)
    await ask(state, "вопрос 2", 2, expect=2)

    assert state.sent[-1]["text"] == "Ответ 2"
    assert await db_admin.fetchval(f"select summary from {S}.clients") is None
    assert await wait_until(lambda: len(llm_state.requests_for("client_summary")) >= 1)
    assert await db_admin.fetchval(
        f"select error from {S}.llm_calls where purpose = 'summary' order by id desc limit 1"
    ) == "http_500"


async def test_memory_does_not_leak_into_logs(fake_tg, llm, start_bot, capsys):
    state, _tg = fake_tg
    llm_state, _ = llm
    await start_bot(log_level="INFO", summary_every_messages=2)
    llm_state.replies += [reply("Здравствуйте!", name="Анна Секретная"), reply("Хорошо")]
    llm_state.summaries.append({"summary": "Клиент Анна Секретная, цель тайная-9731."})

    await ask(state, "меня зовут Анна Секретная", 1, expect=1)
    await ask(state, "и ещё вопрос", 2, expect=2)
    assert await wait_until(lambda: len(llm_state.requests_for("client_summary")) >= 1)

    output = capsys.readouterr().out
    assert "профиль дополнен" in output and '"name"' in output  # видно, что поле заполнено
    assert "Анна Секретная" not in output
    assert "тайная-9731" not in output
