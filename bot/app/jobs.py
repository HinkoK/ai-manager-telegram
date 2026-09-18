"""Фоновая задача: разговоры, которые ждут владельца слишком долго.

Раз в минуту смотрит разговоры в режиме человека. Через handoff_reminder_hours
без ответа напоминает владельцу один раз. Через handoff_return_hours возвращает
разговор боту, помечает его как требующий внимания и сообщает об этом
владельцу. Клиенту при возврате бот ничего не пишет: он не знает, что разговор
куда-то уходил.

Экземпляр бота один, поэтому отдельный планировщик не нужен: задача живёт в том
же процессе и гасится вместе с поллером.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .db import DB_ERRORS
from .handoff import (
    HUMAN_STATES, STATE_REQUESTED, poller_recovered_card, poller_silent_card,
    reminder_card, returned_card,
)
from .repo import Repo
from .telegram import TelegramClient

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    reminded: int = 0
    returned: int = 0
    # True, когда на этом витке владельцу сообщили о молчании или о возврате связи.
    poller_alert: bool = False


class Jobs:
    def __init__(
        self, settings: Settings, repo: Repo, tg: TelegramClient, poller: Any = None
    ) -> None:
        self._s = settings
        self._repo = repo
        self._tg = tg
        # Поллер нужен только чтобы спросить, давно ли отвечал Telegram.
        self._poller = poller
        self._silence_reported = False

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._s.jobs_interval_sec)
            try:
                result = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Фоновая задача не имеет права умереть: без неё разговоры
                # зависнут у владельца навсегда.
                log.exception("фоновая задача упала на витке")
                continue
            if result.reminded or result.returned:
                log.info(
                    "просроченные разговоры обработаны",
                    extra={"reminded": result.reminded, "returned": result.returned},
                )

    async def tick(self) -> TickResult:
        result = TickResult()
        result.poller_alert = await self._check_poller()
        try:
            rows = await self._repo.human_conversations(list(HUMAN_STATES))
        except DB_ERRORS as exc:
            log.error("не прочитать разговоры у владельца", extra={"error": type(exc).__name__})
            return result

        now = datetime.now(timezone.utc)
        for row in rows:
            handoff_at = row["handoff_at"]
            if handoff_at is None:
                continue
            hours = (now - handoff_at).total_seconds() / 3600
            if hours >= self._s.handoff_return_hours:
                if await self._return(row, hours):
                    result.returned += 1
            elif (
                row["state"] == STATE_REQUESTED
                and row["reminded_at"] is None
                and hours >= self._s.handoff_reminder_hours
            ):
                if await self._remind(row, hours):
                    result.reminded += 1
        return result

    async def _check_poller(self) -> bool:
        """Владельцу сообщаем, когда Telegram замолчал и когда снова заговорил.

        Healthcheck такое не ловит: перезапуск контейнера бесполезен, если
        недоступен Telegram, а не завис цикл.
        """
        if self._poller is None or not self._s.poller_silence_minutes:
            return False
        silent_minutes = self._poller.seconds_since_success() / 60
        limit = self._s.poller_silence_minutes

        if silent_minutes >= limit and not self._silence_reported:
            self._silence_reported = True
            log.error("Telegram молчит", extra={"minutes": round(silent_minutes, 1)})
            await self._send(poller_silent_card(limit), 0)
            try:
                await self._repo.add_event(
                    "poller_silent", payload={"minutes": round(silent_minutes, 1)}
                )
            except DB_ERRORS as exc:
                log.error("событие молчания не записано", extra={"error": type(exc).__name__})
            return True

        if silent_minutes < limit and self._silence_reported:
            self._silence_reported = False
            log.info("связь с Telegram восстановилась")
            await self._send(poller_recovered_card(), 0)
            return True
        return False

    async def _remind(self, row: dict, hours: float) -> bool:
        card = reminder_card(
            self._s,
            client=self._client_of(row),
            conversation_id=row["id"],
            reason=row["handoff_reason"],
            hours=self._s.handoff_reminder_hours,
        )
        if not await self._send(card, row["id"]):
            return False
        try:
            await self._repo.mark_reminded(row["id"])
            await self._repo.add_event(
                "owner_reminded",
                conversation_id=row["id"],
                client_id=row["client_id"],
                payload={"hours": round(hours, 1)},
            )
        except DB_ERRORS as exc:
            log.error("напоминание не отмечено", extra={"error": type(exc).__name__})
        return True

    async def _return(self, row: dict, hours: float) -> bool:
        try:
            returned = await self._repo.return_to_bot(row["id"], needs_attention=True)
        except DB_ERRORS as exc:
            log.error("авто-возврат не прошёл", extra={"error": type(exc).__name__})
            return False
        if not returned:
            return False
        await self._send(
            returned_card(
                self._s,
                client=self._client_of(row),
                conversation_id=row["id"],
                hours=self._s.handoff_return_hours,
            ),
            row["id"],
        )
        try:
            await self._repo.add_event(
                "handoff_returned",
                conversation_id=row["id"],
                client_id=row["client_id"],
                payload={"by": "timeout", "hours": round(hours, 1)},
            )
        except DB_ERRORS as exc:
            log.error("событие возврата не записано", extra={"error": type(exc).__name__})
        return True

    @staticmethod
    def _client_of(row: dict) -> dict:
        return {"first_name": row["first_name"], "username": row["username"]}

    async def _send(self, text: str, conversation_id: int) -> bool:
        try:
            await self._tg.send_message(self._s.owner_telegram_id, text)
            return True
        except Exception as exc:
            log.error(
                "сообщение владельцу не доставлено",
                extra={"conversation_id": conversation_id, "error": type(exc).__name__},
            )
            return False
