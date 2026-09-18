"""Лимиты и бюджет: сколько может один клиент и сколько может бот за сутки.

Счётчики живут в базе, а не в памяти процесса: после рестарта лимит не
обнуляется, а один клиент, который написал 200 сообщений, не получает ещё 200
только потому, что контейнер перезапустили. Единственное исключение это
попытки входа в кабинет: их держим в памяти, экземпляр бота один.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import Settings
from .repo import Repo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateCheck:
    allowed: bool
    # Какой лимит сработал: minute, day или None.
    window: str | None = None
    # Сколько сообщений уже было в этом окне.
    count: int = 0


class ClientLimits:
    """Сообщений в минуту и в сутки, длина сообщения.

    Сообщение, которое превысило лимит, в базу уже записано (это улика), но в
    модель и владельцу не идёт. Вежливая пауза уходит один раз на окно, иначе
    бот отвечал бы на каждое из тридцати сообщений в минуту.
    """

    def __init__(self, settings: Settings, repo: Repo) -> None:
        self._s = settings
        self._repo = repo
        # client_id -> когда можно снова сказать «подождите».
        self._paused_until: dict[int, float] = {}

    async def check(self, client_id: int) -> RateCheck:
        per_minute, per_day = await self._repo.client_message_counts(client_id)
        if self._s.client_msgs_per_minute and per_minute > self._s.client_msgs_per_minute:
            return RateCheck(False, "minute", per_minute)
        if self._s.client_msgs_per_day and per_day > self._s.client_msgs_per_day:
            return RateCheck(False, "day", per_day)
        return RateCheck(True)

    def should_warn(self, client_id: int, window: str) -> bool:
        """True один раз на окно: клиенту хватит одного «подождите»."""
        now = time.monotonic()
        if self._paused_until.get(client_id, 0.0) > now:
            return False
        self._paused_until[client_id] = now + (60.0 if window == "minute" else 3600.0)
        # Словарь не растёт бесконечно: чистим протухшие записи по дороге.
        for stale in [k for k, v in self._paused_until.items() if v <= now]:
            del self._paused_until[stale]
        return True

    def too_long(self, text: str | None) -> bool:
        limit = self._s.client_max_message_chars
        return bool(limit) and text is not None and len(text) > limit


class TokenBudget:
    """Дневной бюджет токенов модели. Исчерпан: бот передаёт разговоры
    владельцу и пишет ему об этом один раз в сутки."""

    def __init__(self, settings: Settings, repo: Repo) -> None:
        self._s = settings
        self._repo = repo

    async def exhausted(self) -> tuple[bool, int]:
        limit = self._s.llm_daily_token_budget
        if not limit:
            return False, 0
        used = await self._repo.tokens_last_day()
        return used >= limit, used

    async def alert_needed(self) -> bool:
        """Владельцу пишем один раз в сутки, а не на каждое сообщение."""
        return not await self._repo.event_within("budget_exhausted", hours=24)


class LoginThrottle:
    """Подряд неудачные входы блокируют логин на время. Экземпляр бота один,
    поэтому память процесса тут достаточна; рестарт снимает блокировку."""

    def __init__(self, attempts: int, block_minutes: float) -> None:
        self._attempts = attempts
        self._block_sec = block_minutes * 60
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked_for(self, login: str) -> int:
        """Сколько секунд ждать, 0 если можно пробовать."""
        until = self._blocked_until.get(login, 0.0)
        remaining = until - time.monotonic()
        return int(remaining) + 1 if remaining > 0 else 0

    def failed(self, login: str) -> None:
        count = self._failures.get(login, 0) + 1
        self._failures[login] = count
        if count >= self._attempts:
            self._blocked_until[login] = time.monotonic() + self._block_sec
            self._failures[login] = 0
            log.warning("вход в кабинет заблокирован на время", extra={"login_len": len(login)})

    def succeeded(self, login: str) -> None:
        self._failures.pop(login, None)
        self._blocked_until.pop(login, None)
