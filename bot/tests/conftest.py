from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse, urlunparse

import asyncpg
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from scripts.migrate import apply_migrations
from tests import fake_llm
from tests.fake_telegram import FakeTelegram, build_app

OWNER_ID = 555_000_111
CLIENT_ID = 777_000_222

# Маленькая размерность: фейковым эмбеддингам больше не нужно.
TEST_EMBEDDING_DIM = 64
TEST_LLM_KEY = "test-llm-key-0123456789"
TEST_PROVIDER = {"require_parameters": True, "data_collection": "deny"}

# Тесты ходят только в локальный Postgres из docker-compose.test.yml.
TEST_ADMIN_URL = os.environ.get(
    "TEST_POSTGRES_ADMIN_URL", "postgresql://postgres:test@127.0.0.1:54329/postgres"
)
TEST_SCHEMA = "school_test"
TEST_ROLE = "school_bot_test"
TEST_ROLE_PASSWORD = "school-bot-test-password"
TEST_TABLES = (
    "llm_calls", "messages", "events", "conversations", "clients", "processed_updates",
    "knowledge_chunks", "knowledge_documents",
)


@dataclass(frozen=True)
class DatabaseInfo:
    admin_url: str
    bot_url: str
    schema: str = TEST_SCHEMA
    role: str = TEST_ROLE


class RunningServer:
    def __init__(self, server: uvicorn.Server, task: asyncio.Task, port: int) -> None:
        self.server = server
        self.task = task
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self.server.should_exit = True
        await self.task


async def serve(app: Any) -> RunningServer:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
            # uvicorn глотает падение lifespan и просто выходит.
            raise RuntimeError("сервер не стартовал, смотри лог lifespan")
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return RunningServer(server, task, port)


async def wait_until(predicate: Callable[[], bool], timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def wait_until_async(predicate: Callable[[], Awaitable[bool]], timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return await predicate()


def _bot_url(admin_url: str) -> str:
    parts = urlparse(admin_url)
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{TEST_ROLE}:{quote(TEST_ROLE_PASSWORD)}@{parts.hostname}{port}"
    return urlunparse(parts._replace(netloc=netloc))


@pytest.fixture(scope="session")
def database() -> DatabaseInfo:
    """Схема school_test с нуля на сессию, теми же миграциями, что и продакшен."""

    async def prepare() -> None:
        try:
            conn = await asyncpg.connect(TEST_ADMIN_URL, timeout=5)
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.exit(
                f"нет тестового Postgres ({type(exc).__name__}). Поднять: "
                "docker compose -f docker-compose.test.yml up -d",
                returncode=2,
            )
        try:
            await conn.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
            await apply_migrations(
                conn, TEST_SCHEMA, TEST_ROLE, TEST_EMBEDDING_DIM, report=lambda _line: None
            )
            await conn.execute(
                f"alter role {TEST_ROLE} with login password '{TEST_ROLE_PASSWORD}'"
            )
        finally:
            await conn.close()

    # Отдельный поток со своим циклом: цикл pytest-asyncio этот фикстур не трогает.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, prepare()).result()
    return DatabaseInfo(admin_url=TEST_ADMIN_URL, bot_url=_bot_url(TEST_ADMIN_URL))


@pytest.fixture
async def db_admin(database: DatabaseInfo):
    """Соединение postgres для проверок. Перед каждым тестом таблицы пустые."""
    conn = await asyncpg.connect(database.admin_url)
    tables = ", ".join(f"{TEST_SCHEMA}.{t}" for t in TEST_TABLES)
    await conn.execute(f"truncate {tables} restart identity cascade")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def fake_tg():
    state = FakeTelegram()
    server = await serve(build_app(state))
    try:
        yield state, server
    finally:
        await server.stop()


@pytest.fixture
async def llm():
    state = fake_llm.FakeLLM(dim=TEST_EMBEDDING_DIM)
    server = await serve(fake_llm.build_app(state))
    try:
        yield state, server
    finally:
        await server.stop()


def make_settings(tg_base_url: str, llm_base_url: str, database: DatabaseInfo, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        _env_file=None,
        telegram_bot_token="test-token-0123456789",
        owner_telegram_id=OWNER_ID,
        telegram_api_root=tg_base_url,
        poll_timeout_sec=1,
        log_level="WARNING",
        shutdown_drain_sec=5.0,
        chat_worker_idle_sec=5.0,
        postgres_url=database.bot_url,
        school_schema=database.schema,
        db_pool_min=1,
        db_pool_max=3,
        llm_base_url=llm_base_url,
        llm_api_key=TEST_LLM_KEY,
        llm_model="test/chat-model",
        llm_reasoning_effort="none",
        llm_provider=TEST_PROVIDER,
        llm_timeout_sec=5.0,
        embedding_model="test/embedding-model",
        embedding_dim=TEST_EMBEDDING_DIM,
        embedding_provider={"data_collection": "deny"},
        search_top_k=8,
        search_min_score=0.05,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def start_bot(fake_tg, llm, database, db_admin):
    """Запускает бота с переопределёнными настройками. Можно звать дважды: рестарт."""
    _state, tg_server = fake_tg
    _llm_state, llm_server = llm
    running: list[RunningServer] = []

    async def start(**overrides: Any) -> tuple[RunningServer, Settings]:
        settings = make_settings(tg_server.base_url, llm_server.base_url, database, **overrides)
        server = await serve(create_app(settings))
        running.append(server)
        return server, settings

    try:
        yield start
    finally:
        for server in reversed(running):
            await server.stop()


@pytest.fixture
async def bot(fake_tg, start_bot):
    state, _tg_server = fake_tg
    server, settings = await start_bot()
    yield state, server, settings
