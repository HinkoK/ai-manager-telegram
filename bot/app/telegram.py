"""Тонкий клиент Telegram Bot API. Знает про ошибки, которые нам важны."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Лимит Telegram на текст сообщения.
MAX_TEXT_LEN = 4096


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: float | None = None):
        super().__init__(f"{method} -> {code}: {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def is_conflict(self) -> bool:
        """409: getUpdates уже слушает другой процесс. Long polling терпит только один."""
        return self.code == 409

    @property
    def is_forbidden(self) -> bool:
        """403: пользователь заблокировал бота."""
        return self.code == 403


def clip(text: str, limit: int = MAX_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class TelegramClient:
    def __init__(self, token: str, api_root: str, poll_timeout_sec: int) -> None:
        self._token = token
        self._base = f"{api_root.rstrip('/')}/bot{token}"
        self._poll_timeout = poll_timeout_sec
        # Обычные вызовы короткие. getUpdates держит соединение poll_timeout секунд,
        # поэтому read-таймаут для него считается отдельно.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   *, read_timeout: float | None = None) -> Any:
        timeout = None if read_timeout is None else httpx.Timeout(
            connect=10.0, read=read_timeout, write=15.0, pool=10.0
        )
        response = await self._client.post(
            f"{self._base}/{method}", json=params or {}, timeout=timeout
        )
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(method, response.status_code, "ответ не JSON") from None

        if not payload.get("ok"):
            params_block = payload.get("parameters") or {}
            raise TelegramError(
                method,
                int(payload.get("error_code", response.status_code)),
                str(payload.get("description", "")),
                retry_after=params_block.get("retry_after"),
            )
        return payload.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(self, offset: int, allowed_updates: list[str]) -> list[dict[str, Any]]:
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": self._poll_timeout,
                "allowed_updates": allowed_updates,
            },
            # Telegram закрывает long poll сам; даём запас, чтобы httpx не рвал раньше.
            read_timeout=self._poll_timeout + 15,
        )

    async def send_message(self, chat_id: int, text: str,
                           reply_to_message_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": clip(text)}
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        return await self.call("sendMessage", params)
