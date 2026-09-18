"""Цикл long polling.

Три решения, которые стоит держать в голове.

1. Offset подтверждается только после того, как весь батч обработан. Telegram
   забывает update в тот момент, когда мы просим следующий offset. Подтвердить
   раньше значит потерять сообщение при падении процесса. Цена: медленный ответ
   одному клиенту задерживает приём новых сообщений. Для школы это допустимо.
2. Heartbeat обновляется на каждом витке цикла, включая витки с ошибкой. Так
   healthcheck ловит зависший поллер, а не недоступный Telegram: перезапуск
   контейнера чинит первое и бесполезен против второго.
3. Повторы update отсекает обработчик по таблице processed_updates, а не
   память поллера. После рестарта память пуста, а таблица нет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Sequence

import httpx

from .dispatcher import ChatDispatcher
from .handlers import UpdateHandler
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

MAX_BACKOFF_SEC = 30.0
CONFLICT_PAUSE_SEC = 30.0


class Poller:
    def __init__(
        self,
        tg: TelegramClient,
        dispatcher: ChatDispatcher,
        handler: UpdateHandler,
        allowed_updates: Sequence[str] = ("message",),
    ) -> None:
        self._tg = tg
        self._dispatcher = dispatcher
        self._handler = handler
        self._allowed = list(allowed_updates)
        self._offset = 0
        self._last_tick = time.monotonic()
        self._started_at = time.monotonic()
        self._last_success: float | None = None

    def seconds_since_tick(self) -> float:
        return time.monotonic() - self._last_tick

    def seconds_since_success(self) -> float:
        """Сколько Telegram не отвечает. Пока молчание не началось, 0.

        Long poll возвращается сам раз в poll_timeout секунд, даже когда
        сообщений нет, поэтому растущее значение означает обрыв связи.
        """
        if self._last_success is None:
            return time.monotonic() - self._started_at
        return time.monotonic() - self._last_success

    @property
    def talked_to_telegram(self) -> bool:
        return self._last_success is not None

    async def run(self) -> None:
        backoff = 1.0
        while True:
            self._last_tick = time.monotonic()
            try:
                updates = await self._tg.get_updates(self._offset, self._allowed)
            except TelegramError as exc:
                backoff = await self._on_telegram_error(exc, backoff)
                continue
            except (httpx.HTTPError, OSError) as exc:
                log.warning(
                    "Telegram недоступен, повтор",
                    extra={"error": type(exc).__name__, "sleep": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                continue

            self._last_success = time.monotonic()
            backoff = 1.0
            if not updates:
                continue

            highest = self._offset - 1
            for update in updates:
                highest = max(highest, int(update["update_id"]))
                await self._submit(update)

            # Ждём, пока батч доработает, и только теперь подтверждаем offset.
            await self._dispatcher.wait_idle()
            self._offset = highest + 1

    async def _on_telegram_error(self, exc: TelegramError, backoff: float) -> float:
        if exc.is_conflict:
            # 409: getUpdates слушает кто-то ещё. Обычно это второй экземпляр
            # контейнера или запущенный локально бот с тем же токеном.
            log.error(
                "getUpdates занят другим процессом, long polling терпит только один",
                extra={"sleep": CONFLICT_PAUSE_SEC},
            )
            await asyncio.sleep(CONFLICT_PAUSE_SEC)
            return backoff
        if exc.code == 429:
            pause = float(exc.retry_after or 5)
            log.warning("Telegram просит подождать", extra={"sleep": pause})
            await asyncio.sleep(pause)
            return backoff
        log.error(
            "ошибка getUpdates",
            extra={"code": exc.code, "description": exc.description, "sleep": backoff},
        )
        await asyncio.sleep(backoff)
        return min(backoff * 2, MAX_BACKOFF_SEC)

    async def _submit(self, update: dict[str, Any]) -> None:
        chat_id = self._handler.chat_id_of(update)
        if chat_id is None:
            return

        async def job() -> None:
            await self._handler.handle(update)

        await self._dispatcher.submit(chat_id, job)
