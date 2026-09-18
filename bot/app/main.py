"""Точка входа: FastAPI держит поллер и отдаёт healthcheck.

Поллер живёт внутри этого же процесса, потому что Telegram разрешает только один
открытый getUpdates на токен. Второй экземпляр получит 409, поэтому контейнер
запускается в одном экземпляре, без реплик.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlparse

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .answer import AnswerService
from .api import build_router
from .config import Settings, load_settings
from .db import DB_ERRORS, Database
from .dispatcher import ChatDispatcher
from .handlers import UpdateHandler
from .ingest import check_knowledge_schema
from .jobs import Jobs
from .limits import ClientLimits, LoginThrottle, TokenBudget
from .llm import OpenAICompatClient
from .memory import MemoryService
from .owner import OwnerHandler, OwnerService
from .logging_setup import setup_logging
from .poller import Poller
from .repo import Repo
from .telegram import TelegramClient

log = logging.getLogger(__name__)

T = TypeVar("T")

STARTUP_ATTEMPTS = 5

# Эти ошибки повтором не лечатся. Неверный пароль к тому же опасно повторять:
# пулер Supabase банит адрес за серию неудачных входов.
NOT_RETRIED: tuple[type[BaseException], ...] = (
    asyncpg.InvalidAuthorizationSpecificationError,
)


async def _with_retry(what: str, call: Callable[[], Awaitable[T]]) -> T:
    """Короткий сбой сети не валит старт, постоянная ошибка валит."""
    delay = 1.0
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            return await call()
        except NOT_RETRIED:
            raise
        except Exception as exc:
            if attempt == STARTUP_ATTEMPTS:
                raise
            log.warning(
                "шаг старта не прошёл, повтор",
                extra={"step": what, "attempt": attempt, "error": type(exc).__name__},
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    raise RuntimeError("недостижимо")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db_password = urlparse(settings.postgres_url).password or ""
    setup_logging(
        settings.log_level,
        secrets=[settings.telegram_bot_token, db_password, settings.llm_api_key, settings.embedding_key],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(
            settings.postgres_url,
            settings.school_schema,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
        tg = TelegramClient(
            token=settings.telegram_bot_token,
            api_root=settings.telegram_api_root,
            poll_timeout_sec=settings.poll_timeout_sec,
        )
        llm = OpenAICompatClient(settings.llm_base_url, settings.llm_api_key, settings.llm_timeout_sec)
        embedder = OpenAICompatClient(settings.embedding_url, settings.embedding_key, settings.llm_timeout_sec)

        async def close_clients() -> None:
            await tg.aclose()
            await llm.aclose()
            await embedder.aclose()
            await db.close()

        repo = Repo(db)
        try:
            await _with_retry("база", db.connect)
            models = await check_knowledge_schema(repo, settings)
            me = await _with_retry("getMe", tg.get_me)
        except BaseException:
            await close_clients()
            raise
        if not models:
            log.warning("база знаний пуста: бот будет передавать руководителю любой вопрос, "
                        "загрузите её командой python -m app.ingest")

        try:
            purged = await repo.purge_old_updates(settings.processed_updates_ttl_days)
            sessions = await repo.purge_sessions()
            log.info("старое почищено", extra={"updates": purged, "sessions": sessions})
        except DB_ERRORS as exc:
            log.warning("не удалось почистить processed_updates", extra={"error": type(exc).__name__})

        dispatcher = ChatDispatcher(idle_sec=settings.chat_worker_idle_sec)
        answers = AnswerService(settings, repo, llm, embedder)
        memory = MemoryService(settings, repo, llm)
        owner_service = OwnerService(tg, settings, repo)
        owner = OwnerHandler(tg, settings, repo, owner_service)
        handler = UpdateHandler(
            tg, settings, repo, answers, memory, owner,
            ClientLimits(settings, repo), TokenBudget(settings, repo),
        )
        # Кабинет ходит в эти же маршруты, поэтому роутер собирается здесь, где
        # уже есть пул базы и клиент Telegram.
        throttle = LoginThrottle(settings.cabinet_login_attempts, settings.cabinet_login_block_minutes)
        app.include_router(build_router(settings, repo, owner_service, throttle))
        poller = Poller(tg, dispatcher, handler)
        jobs = Jobs(settings, repo, tg, poller)
        app.state.poller = poller
        app.state.bot_username = me.get("username")
        task = asyncio.create_task(poller.run(), name="poller")
        jobs_task = asyncio.create_task(jobs.run(), name="jobs")
        log.info("бот запущен", extra={"bot_username": me.get("username")})
        try:
            yield
        finally:
            # Отменяем открытый long poll: неподтверждённые update Telegram
            # передоставит. Потом даём доработать уже начатым обработчикам.
            # База закрывается последней: обработчикам она нужна до конца.
            task.cancel()
            jobs_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(asyncio.CancelledError):
                await jobs_task
            await dispatcher.close(timeout=settings.shutdown_drain_sec)
            await close_clients()
            log.info("бот остановлен")

    app = FastAPI(
        title="school ai manager bot",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def limit_body_size(request, call_next):
        # Тело больше лимита отклоняем до разбора JSON: Caddy появится только на
        # этапе 9, а до него API открыт напрямую.
        if request.url.path.startswith("/api"):
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > settings.api_max_body_bytes:
                return JSONResponse({"detail": "слишком большой запрос"}, status_code=413)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        poller: Poller | None = getattr(app.state, "poller", None)
        if poller is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        age = poller.seconds_since_tick()
        limit = settings.poll_timeout_sec + 30
        healthy = age < limit
        return JSONResponse(
            {
                "status": "ok" if healthy else "stale",
                "seconds_since_tick": round(age, 1),
                "talked_to_telegram": poller.talked_to_telegram,
            },
            status_code=200 if healthy else 503,
        )

    return app
