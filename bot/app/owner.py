"""Ветка владельца: ответы клиентам, возврат разговора боту, /status.

Разговор определяется реплаем на уведомление. Если владелец пишет без реплая,
а разговор у него ровно один, доставляем в него. Когда разговоров несколько,
угадывать нельзя, и бот просит реплай.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import texts
from .config import Settings
from .db import DB_ERRORS
from .handoff import HUMAN_STATES, status_card
from .repo import Repo
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    conversation_id: int
    client_id: int
    client_telegram_id: int


class OwnerService:
    """Действия владельца над разговором. Одни и те же для Telegram и кабинета."""

    def __init__(self, tg: TelegramClient, settings: Settings, repo: Repo) -> None:
        self._tg = tg
        self._settings = settings
        self._repo = repo

    async def deliver(self, target: Target, text: str) -> str:
        """sent, blocked или failed. Недоставленное в историю не пишем."""
        try:
            sent = await self._tg.send_message(target.client_telegram_id, text)
        except TelegramError as exc:
            log.error(
                "ответ владельца не доставлен",
                extra={"conversation_id": target.conversation_id, "code": exc.code},
            )
            return "blocked" if exc.is_forbidden else "failed"
        except Exception as exc:
            log.error(
                "сбой отправки ответа владельца",
                extra={"conversation_id": target.conversation_id, "error": type(exc).__name__},
            )
            return "failed"

        try:
            async with self._repo.tx() as conn:
                await self._repo.insert_message(
                    conn,
                    conversation_id=target.conversation_id,
                    client_id=target.client_id,
                    role="owner",
                    text=text,
                    telegram_message_id=sent.get("message_id"),
                )
                await self._repo.touch_conversation(conn, target.conversation_id)
                await self._repo.add_event(
                    "owner_reply",
                    conversation_id=target.conversation_id,
                    client_id=target.client_id,
                    conn=conn,
                )
            # Владелец заговорил сам: разговор больше не ждёт первого ответа.
            await self._repo.owner_took_conversation(target.conversation_id)
        except DB_ERRORS as exc:
            # Клиент текст уже получил, потеряна только запись.
            log.error("ответ владельца не записан", extra={"error": type(exc).__name__})
        log.info("ответ владельца доставлен", extra={"conversation_id": target.conversation_id})
        return "sent"

    async def return_to_bot(self, target: Target, by: str) -> bool:
        returned = await self._repo.return_to_bot(target.conversation_id)
        if returned:
            await self._repo.add_event(
                "handoff_returned",
                conversation_id=target.conversation_id,
                client_id=target.client_id,
                payload={"by": by},
            )
        log.info(
            "разговор возвращён боту",
            extra={"conversation_id": target.conversation_id, "changed": returned, "by": by},
        )
        return returned


class OwnerHandler:
    def __init__(
        self, tg: TelegramClient, settings: Settings, repo: Repo, service: OwnerService
    ) -> None:
        self._tg = tg
        self._settings = settings
        self._repo = repo
        self._service = service

    async def handle(self, message: dict[str, Any]) -> None:
        s = self._settings
        chat_id = int(message["chat"]["id"])
        text = message.get("text")
        reply_to = (message.get("reply_to_message") or {}).get("message_id")

        if text is None:
            await self._say(chat_id, texts.owner_non_text(s))
            return
        command = text.split(maxsplit=1)[0].lower()
        if command.startswith("/start"):
            await self._say(chat_id, texts.owner_greeting(s))
            return
        if command.startswith("/status"):
            human, requested, new_leads = await self._repo.status_counts()
            await self._say(chat_id, status_card(human=human, requested=requested, new_leads=new_leads))
            return
        if command.startswith("/bot"):
            await self._return_to_bot(chat_id, reply_to)
            return
        if command in ("/ban", "/unban"):
            await self._set_ban(chat_id, reply_to, banned=command == "/ban")
            return
        if command.startswith("/"):
            await self._say(chat_id, texts.owner_unknown_command(s))
            return
        await self._deliver(chat_id, reply_to, text)

    async def _target(self, reply_to: int | None) -> Target | str:
        """Разговор для ответа владельца или причина, почему его не нашли."""
        if reply_to is not None:
            found = await self._repo.conversation_by_relay(reply_to)
            if found is None:
                return "not_found"
            return Target(found["id"], found["client_id"], found["telegram_user_id"])
        active = await self._repo.human_conversations(list(HUMAN_STATES))
        if not active:
            return "none"
        if len(active) > 1:
            return "many"
        only = active[0]
        return Target(only["id"], only["client_id"], only["telegram_user_id"])

    async def _deliver(self, chat_id: int, reply_to: int | None, text: str) -> None:
        s = self._settings
        try:
            target = await self._target(reply_to)
        except DB_ERRORS as exc:
            log.error("не найден разговор для ответа", extra={"error": type(exc).__name__})
            await self._say(chat_id, texts.owner_temporary_problem(s))
            return
        if isinstance(target, str):
            await self._say(chat_id, texts.owner_no_target(s, target))
            return

        result = await self._service.deliver(target, text)
        if result != "sent":
            await self._say(chat_id, texts.owner_not_delivered(s, result == "blocked"))
            return
        await self._say(chat_id, texts.owner_delivered(s, target.conversation_id))

    async def _return_to_bot(self, chat_id: int, reply_to: int | None) -> None:
        s = self._settings
        try:
            target = await self._target(reply_to)
            if isinstance(target, str):
                await self._say(chat_id, texts.owner_no_target(s, target))
                return
            returned = await self._service.return_to_bot(target, by="owner")
        except DB_ERRORS as exc:
            log.error("возврат боту не прошёл", extra={"error": type(exc).__name__})
            await self._say(chat_id, texts.owner_temporary_problem(s))
            return
        await self._say(chat_id, texts.owner_returned(s, target.conversation_id, returned))

    async def _set_ban(self, chat_id: int, reply_to: int | None, *, banned: bool) -> None:
        """Бан по реплаю на уведомление: бот перестаёт читать этого клиента."""
        s = self._settings
        try:
            target = await self._target(reply_to)
            if isinstance(target, str):
                await self._say(chat_id, texts.owner_no_target(s, target))
                return
            changed = await self._repo.set_banned(
                target.client_id, banned, reason="владелец, из Telegram" if banned else None
            )
            if changed:
                await self._repo.add_event(
                    "client_banned" if banned else "client_unbanned",
                    conversation_id=target.conversation_id,
                    client_id=target.client_id,
                    payload={"by": "owner"},
                )
        except DB_ERRORS as exc:
            log.error("бан не прошёл", extra={"error": type(exc).__name__})
            await self._say(chat_id, texts.owner_temporary_problem(s))
            return
        log.info("бан клиента изменён", extra={"client_id": target.client_id, "banned": banned})
        await self._say(chat_id, texts.owner_ban_result(s, target.conversation_id, banned, changed))

    async def _say(self, chat_id: int, text: str) -> None:
        try:
            await self._tg.send_message(chat_id, text)
        except Exception as exc:
            log.error("не удалось ответить владельцу", extra={"error": type(exc).__name__})
