"""Фейковый Telegram Bot API как настоящий HTTP-сервер.

Не подменяем httpx: поднимаем сервер и проверяем реальный клиент, реальные
таймауты и реальную семантику offset. Единственное отличие от Telegram - long
poll держится не дольше пары секунд, чтобы тесты не стояли.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_FAKE_LONG_POLL_SEC = 2.0


class FakeTelegram:
    def __init__(self, bot_username: str = "school_test_bot") -> None:
        self.bot_username = bot_username
        self.pending: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        # Чаты, где пользователь заблокировал бота: sendMessage туда получит 403.
        self.forbidden_chats: set[int] = set()
        self.get_updates_calls = 0
        self._next_message_id = 1000

    def push(self, update: dict[str, Any]) -> None:
        self.pending.append(update)

    def confirm(self, offset: int) -> None:
        """Как настоящий Telegram: offset подтверждает всё, что меньше него."""
        if offset > 0:
            self.pending = [u for u in self.pending if int(u["update_id"]) >= offset]

    def texts_to(self, chat_id: int) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == chat_id]


def _ok(result: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "result": result})


def build_app(state: FakeTelegram) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/bot{token}/{method}")
    async def call(token: str, method: str, request: Request) -> JSONResponse:
        body = await request.json()

        if method == "getMe":
            return _ok(
                {"id": 42, "is_bot": True, "username": state.bot_username, "first_name": "test"}
            )

        if method == "getUpdates":
            state.get_updates_calls += 1
            offset = int(body.get("offset") or 0)
            state.confirm(offset)
            timeout = float(body.get("timeout") or 0)
            deadline = time.monotonic() + min(timeout, MAX_FAKE_LONG_POLL_SEC)
            while True:
                available = [u for u in state.pending if int(u["update_id"]) >= offset]
                if available:
                    return _ok(available)
                if time.monotonic() >= deadline:
                    return _ok([])
                await asyncio.sleep(0.02)

        if method == "sendMessage":
            if int(body["chat_id"]) in state.forbidden_chats:
                return JSONResponse(
                    {
                        "ok": False,
                        "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user",
                    },
                    status_code=403,
                )
            state._next_message_id += 1
            record = {
                "chat_id": int(body["chat_id"]),
                "text": body["text"],
                "message_id": state._next_message_id,
            }
            state.sent.append(record)
            return _ok(
                {
                    "message_id": state._next_message_id,
                    "chat": {"id": record["chat_id"], "type": "private"},
                    "text": record["text"],
                }
            )

        return JSONResponse(
            {"ok": False, "error_code": 400, "description": f"неизвестный метод {method}"},
            status_code=400,
        )

    return app


def message_update(
    update_id: int,
    *,
    user_id: int,
    text: str | None = "привет",
    chat_id: int | None = None,
    chat_type: str = "private",
    is_bot: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 1_700_000_000,
        "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Тест"},
    }
    if text is not None:
        message["text"] = text
    if extra:
        message.update(extra)
    return {"update_id": update_id, "message": message}
