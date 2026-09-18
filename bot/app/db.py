"""Пул соединений и транзакции.

Драйвер asyncpg, режим сессионный. У Supabase два пулера: транзакционный на
6543 ломает подготовленные выражения, и asyncpg на нём требует отключать кеш;
сессионный на 5432 работает как обычный Postgres. Нам транзакционный не нужен:
бот это один долгоживущий процесс с пулом на несколько соединений.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

log = logging.getLogger(__name__)

SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Всё, что значит «база недоступна или ответила ошибкой». Обработчик ловит это,
# пишет клиенту нейтральный текст и живёт дальше, а не роняет очередь чата.
DB_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    asyncio.TimeoutError,
)


def check_identifier(name: str, what: str) -> str:
    """Имя схемы подставляется в SQL текстом, поэтому проверяем его строго."""
    if not SAFE_IDENT.match(name):
        raise ValueError(f"{what}={name!r}: только строчные буквы, цифры и подчёркивание")
    return name


class Database:
    def __init__(self, dsn: str, schema: str, min_size: int = 1, max_size: int = 5) -> None:
        self._dsn = dsn
        self.schema = check_identifier(schema, "SCHOOL_SCHEMA")
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min,
            max_size=self._max,
            command_timeout=10,
            server_settings={"application_name": "school-ai-manager"},
        )
        try:
            async with pool.acquire() as conn:
                version = await conn.fetchval("select current_setting('server_version')")
        except BaseException:
            # Повторная попытка старта создаст новый пул, этот не должен висеть.
            await pool.close()
            raise
        self._pool = pool
        log.info("база подключена", extra={"schema": self.schema, "server_version": version})

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("пул не поднят")
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn
