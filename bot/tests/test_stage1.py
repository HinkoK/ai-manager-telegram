"""Этап 1: транспорт и конкурентность.

Базы и модели ещё нет, поэтому проверяем ровно то, что построено: маршрутизацию,
порядок, дедуп и остановку.
"""

from __future__ import annotations

import asyncio

import httpx

from app.dispatcher import ChatDispatcher
from tests.conftest import CLIENT_ID, OWNER_ID, wait_until
from tests.fake_telegram import message_update


async def test_start_greets_client(bot):
    state, _server, settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="/start"))

    assert await wait_until(lambda: len(state.sent) >= 1)
    await asyncio.sleep(0.2)

    assert len(state.sent) == 1
    reply = state.sent[0]
    assert reply["chat_id"] == CLIENT_ID
    assert settings.school_name in reply["text"]
    assert "пробный" in reply["text"].lower()


async def test_bot_is_silent_in_groups(bot):
    state, _server, _settings = bot
    state.push(
        message_update(1, user_id=CLIENT_ID, chat_id=-100_123, chat_type="supergroup", text="/start")
    )

    await asyncio.sleep(1.0)
    assert state.sent == []


async def test_messages_from_other_bots_ignored(bot):
    state, _server, _settings = bot
    state.push(message_update(1, user_id=999_111, text="привет", is_bot=True))

    await asyncio.sleep(1.0)
    assert state.sent == []


async def test_duplicate_update_answered_once(bot):
    """Telegram может прислать один update дважды. Отвечаем один раз."""
    state, _server, _settings = bot
    original = message_update(7, user_id=CLIENT_ID, text="сколько стоит")
    state.push(original)
    state.push(dict(original))

    assert await wait_until(lambda: len(state.sent) >= 1)
    await asyncio.sleep(0.4)
    assert len(state.sent) == 1


async def test_two_messages_answered_in_order(bot):
    state, _server, settings = bot
    state.push(message_update(1, user_id=CLIENT_ID, text="/start"))
    state.push(message_update(2, user_id=CLIENT_ID, text="а сколько стоит"))

    assert await wait_until(lambda: len(state.sent) >= 2)
    await asyncio.sleep(0.2)

    texts = state.texts_to(CLIENT_ID)
    assert len(texts) == 2
    assert settings.school_name in texts[0]
    assert "Фейковый ответ модели" in texts[1]


async def test_owner_is_recognised(bot):
    state, _server, _settings = bot
    state.push(message_update(1, user_id=OWNER_ID, text="/start"))

    assert await wait_until(lambda: len(state.sent) >= 1)
    assert "владелец" in state.sent[0]["text"].lower()


async def test_non_text_gets_polite_refusal(bot):
    state, _server, _settings = bot
    state.push(
        message_update(
            1,
            user_id=CLIENT_ID,
            text=None,
            extra={"voice": {"file_id": "abc", "duration": 3, "file_unique_id": "u"}},
        )
    )

    assert await wait_until(lambda: len(state.sent) >= 1)
    assert "только текст" in state.sent[0]["text"].lower()


async def test_healthz_is_green_while_polling(bot):
    state, server, _settings = bot
    # Второй запрос getUpdates возможен только после того, как вернулся первый:
    # так мы дожидаемся успешного ответа, а не только факта отправки.
    assert await wait_until(lambda: state.get_updates_calls >= 2)

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{server.base_url}/healthz", timeout=5)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["talked_to_telegram"] is True


async def test_offset_confirmed_only_after_batch(bot):
    """Подтверждённый update исчезает из очереди Telegram, неподтверждённый нет."""
    state, _server, _settings = bot
    state.push(message_update(5, user_id=CLIENT_ID, text="первый вопрос"))

    assert await wait_until(lambda: len(state.sent) >= 1)
    assert await wait_until(lambda: state.pending == [])


async def test_dispatcher_keeps_order_within_chat():
    """Медленная задача не пропускает вперёд быструю в том же чате."""
    dispatcher = ChatDispatcher(idle_sec=5.0)
    done: list[int] = []

    async def job(number: int, delay: float) -> None:
        await asyncio.sleep(delay)
        done.append(number)

    await dispatcher.submit(1, lambda: job(1, 0.10))
    await dispatcher.submit(1, lambda: job(2, 0.0))
    await dispatcher.submit(1, lambda: job(3, 0.0))
    await dispatcher.wait_idle()

    assert done == [1, 2, 3]
    await dispatcher.close(timeout=2.0)


async def test_dispatcher_runs_chats_in_parallel():
    dispatcher = ChatDispatcher(idle_sec=5.0)
    done: list[int] = []

    async def job(number: int, delay: float) -> None:
        await asyncio.sleep(delay)
        done.append(number)

    await dispatcher.submit(1, lambda: job(1, 0.15))
    await dispatcher.submit(2, lambda: job(2, 0.0))
    await dispatcher.wait_idle()

    assert done == [2, 1]
    await dispatcher.close(timeout=2.0)
