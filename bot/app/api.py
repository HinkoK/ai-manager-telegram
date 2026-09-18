"""REST API кабинета владельца.

Кабинет ходит только сюда, в базу напрямую он не смотрит. Владелец один,
регистрации нет: логин и хеш пароля лежат в .env, сессия в httpOnly cookie,
в базе только хеш токена.

Все маршруты, кроме входа, требуют живую сессию. Мутации принимают только JSON
и запрещены с чужого источника простой формой: cookie помечена SameSite=Lax.
Полноценная защита от CSRF и лимит попыток входа это этап 8.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth import new_token, token_hash, verify_password
from .config import Settings
from .handoff import HUMAN_STATES, reason_name
from .leads import KINDS
from .limits import LoginThrottle
from .owner import OwnerService, Target
from .repo import Repo

log = logging.getLogger(__name__)

COOKIE_NAME = "school_session"
LEAD_STATUSES = ("new", "in_progress", "done", "spam")
CONVERSATION_FILTERS = ("attention", "human", "bot", "all")


class LoginBody(BaseModel):
    login: str = Field(max_length=200)
    password: str = Field(max_length=500)


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class LeadPatch(BaseModel):
    status: Literal["new", "in_progress", "done", "spam"] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ClientPatch(BaseModel):
    is_banned: bool


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    return parsed.hostname


def same_origin(request: Request, settings: Settings) -> bool:
    """Мутации только со своего сайта.

    Основная защита от CSRF это SameSite=Lax у cookie: чужой сайт не может
    отправить POST с нашей сессией. Эта проверка вторая ограда на случай
    старых браузеров: Origin запроса должен совпадать с хостом кабинета.
    Порт не сравниваем: в разработке кабинет на 3000, а API на 8000.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = _hostname(request.headers.get("origin"))
    if origin is None:
        return True  # не браузер или same-origin запрос без заголовка
    allowed = {
        _hostname(request.headers.get("x-forwarded-host")),
        _hostname(request.headers.get("host")),
        _hostname(settings.cabinet_url),
    }
    return origin in allowed


def _json(value: Any) -> Any:
    """asyncpg отдаёт jsonb строкой, кабинету нужен объект."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _client(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("client_id"),
        "telegram_user_id": row.get("telegram_user_id"),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
    }


def build_router(
    settings: Settings, repo: Repo, service: OwnerService, throttle: LoginThrottle
) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def require_session(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> str:
        if not session or not await repo.session_alive(token_hash(session)):
            raise HTTPException(status_code=401, detail="нужен вход")
        return session

    async def require_same_origin(request: Request) -> None:
        if not same_origin(request, settings):
            log.warning("мутация с чужого источника отклонена", extra={"path": request.url.path})
            raise HTTPException(status_code=403, detail="запрос с чужого сайта")

    guarded = [Depends(require_session)]
    mutation = [Depends(require_session), Depends(require_same_origin)]

    @router.post("/login", dependencies=[Depends(require_same_origin)])
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not settings.cabinet_password_hash:
            # Пароль не задан: лучше честно сказать, чем пускать без пароля.
            raise HTTPException(status_code=503, detail="кабинет не настроен: нет пароля")
        wait = throttle.blocked_for(body.login)
        if wait:
            raise HTTPException(
                status_code=429,
                detail="слишком много неверных попыток, подождите",
                headers={"Retry-After": str(wait)},
            )
        # Обе проверки считаются всегда: по времени ответа нельзя понять, верен
        # ли логин. Сравнение логина тоже постоянного времени.
        login_ok = hmac.compare_digest(body.login.encode(), settings.cabinet_login.encode())
        password_ok = verify_password(body.password, settings.cabinet_password_hash)
        if not (login_ok and password_ok):
            throttle.failed(body.login)
            log.warning("неудачный вход в кабинет", extra={"login_len": len(body.login)})
            raise HTTPException(status_code=401, detail="неверный логин или пароль")
        throttle.succeeded(body.login)

        token = new_token()
        await repo.create_session(
            token_hash(token), settings.cabinet_session_days, request.headers.get("user-agent")
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.cabinet_session_days * 24 * 3600,
            httponly=True,
            secure=settings.cabinet_cookie_secure,
            samesite="lax",
            path="/",
        )
        log.info("вход в кабинет")
        return {"login": settings.cabinet_login}

    @router.post("/logout", dependencies=[Depends(require_same_origin)])
    async def logout(response: Response, session: str = Depends(require_session)) -> dict[str, bool]:
        await repo.revoke_session(token_hash(session))
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @router.get("/me", dependencies=guarded)
    async def me() -> dict[str, Any]:
        human, requested, new_leads = await repo.status_counts()
        return {
            "login": settings.cabinet_login,
            "school": settings.school_name,
            "counters": {"human": human, "requested": requested, "new_leads": new_leads},
        }

    @router.get("/conversations", dependencies=guarded)
    async def conversations(filter: str = "all", limit: int = 50) -> dict[str, Any]:
        if filter not in CONVERSATION_FILTERS:
            raise HTTPException(status_code=400, detail="неизвестный фильтр")
        rows = await repo.conversations(None if filter == "all" else filter, min(limit, 200))
        return {
            "items": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "status": r["status"],
                    "needs_attention": r["needs_attention"],
                    "unread": r["unread"],
                    "handoff_reason": r["handoff_reason"],
                    "last_message_at": r["last_message_at"],
                    "last_text": r["last_text"],
                    "open_leads": r["open_leads"],
                    "client": _client(r),
                }
                for r in rows
            ]
        }

    @router.get("/conversations/{conversation_id}", dependencies=guarded)
    async def conversation(conversation_id: int) -> dict[str, Any]:
        row = await repo.conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="разговор не найден")
        messages = await repo.conversation_messages(conversation_id)
        leads = await repo.leads_of_client(row["client_id"])
        # Прочитанным разговор делает кнопка в кабинете, а не сам факт запроса:
        # иначе опрос списка раз в пять секунд снимал бы пометки сам.
        return {
            "id": row["id"],
            "state": row["state"],
            "status": row["status"],
            "handoff_reason": row["handoff_reason"],
            "handoff_reason_name": reason_name(row["handoff_reason"]) if row["handoff_reason"] else None,
            "handoff_at": row["handoff_at"],
            "returned_at": row["returned_at"],
            "started_at": row["started_at"],
            "last_message_at": row["last_message_at"],
            "needs_attention": row["needs_attention"],
            "client": {
                **_client(row),
                "profile": _json(row["profile"]),
                "summary": row["summary"],
                "created_at": row["client_created_at"],
            },
            "events": [
                {"type": e["type"], "title": _event_title(e), "created_at": e["created_at"]}
                for e in await repo.conversation_events(conversation_id)
            ],
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "text": m["text"],
                    "created_at": m["created_at"],
                    "meta": _json(m["meta"]),
                }
                for m in messages
            ],
            "leads": [_lead(l) for l in leads],
        }

    @router.post("/conversations/{conversation_id}/seen", dependencies=mutation)
    async def mark_seen(conversation_id: int) -> dict[str, Any]:
        row = await repo.conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="разговор не найден")
        await repo.mark_seen(conversation_id)
        return {"ok": True, "unread": False, "needs_attention": False}

    @router.post("/conversations/{conversation_id}/reply", dependencies=mutation)
    async def reply(conversation_id: int, body: ReplyBody) -> dict[str, Any]:
        row = await repo.conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="разговор не найден")
        target = Target(row["id"], row["client_id"], row["telegram_user_id"])
        result = await service.deliver(target, body.text.strip())
        if result == "blocked":
            raise HTTPException(status_code=409, detail="клиент заблокировал бота")
        if result != "sent":
            raise HTTPException(status_code=502, detail="Telegram не принял сообщение")
        return {"ok": True, "state": "HUMAN_ACTIVE"}

    @router.post("/conversations/{conversation_id}/return", dependencies=mutation)
    async def return_to_bot(conversation_id: int) -> dict[str, Any]:
        row = await repo.conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="разговор не найден")
        target = Target(row["id"], row["client_id"], row["telegram_user_id"])
        changed = await service.return_to_bot(target, by="cabinet")
        return {"ok": True, "changed": changed, "state": "AI_ACTIVE"}

    @router.get("/leads", dependencies=guarded)
    async def leads(status: str = "all", limit: int = 100) -> dict[str, Any]:
        if status != "all" and status not in LEAD_STATUSES:
            raise HTTPException(status_code=400, detail="неизвестный статус")
        rows = await repo.leads(None if status == "all" else status, min(limit, 200))
        return {"items": [_lead(r) for r in rows]}

    @router.patch("/leads/{lead_id}", dependencies=mutation)
    async def patch_lead(lead_id: int, body: LeadPatch) -> dict[str, Any]:
        if body.status is None and body.notes is None:
            raise HTTPException(status_code=400, detail="нечего менять")
        row = await repo.update_lead_card(lead_id, body.status, body.notes)
        if row is None:
            raise HTTPException(status_code=404, detail="заявка не найдена")
        log.info("заявка изменена из кабинета", extra={"lead_id": lead_id, "status": row["status"]})
        return _lead(row)

    @router.patch("/clients/{client_id}", dependencies=mutation)
    async def patch_client(client_id: int, body: ClientPatch) -> dict[str, Any]:
        """Бан из кабинета. Забаненного клиента бот не читает и не отвечает."""
        changed = await repo.set_banned(
            client_id, body.is_banned, reason="владелец, из кабинета" if body.is_banned else None
        )
        if not changed:
            raise HTTPException(status_code=404, detail="клиент не найден")
        await repo.add_event(
            "client_banned" if body.is_banned else "client_unbanned",
            client_id=client_id,
            payload={"by": "cabinet"},
        )
        log.info("бан клиента изменён", extra={"client_id": client_id, "banned": body.is_banned})
        return {"id": client_id, "is_banned": body.is_banned}

    return router


def _event_title(event: dict[str, Any]) -> str:
    """Строка события для переписки в кабинете, уже по-русски."""
    payload = _json(event.get("payload")) or {}
    if event["type"] == "handoff_started":
        return f"Разговор передан вам: {reason_name(payload.get('reason'))}"
    if event["type"] == "handoff_returned":
        by = {"owner": "вы вернули разговор боту", "cabinet": "вы вернули разговор боту из кабинета"}
        return by.get(payload.get("by"), "разговор вернулся боту: ответа не было сутки")
    return "Напоминание отправлено вам в Telegram"


def _lead(row: dict[str, Any]) -> dict[str, Any]:
    fields = {
        key: row.get(key)
        for key in ("goal", "level", "format", "timezone", "preferred_time", "company",
                    "team_size", "sphere", "notes")
    }
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "is_qualified": row["is_qualified"],
        "conversation_id": row.get("conversation_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "client": _client(row),
        "fields": fields,
    }


# Проверка на старте: виды заявок и статусы кабинета совпадают с базой.
assert set(LEAD_STATUSES) >= {"new", "in_progress", "done", "spam"}
assert set(KINDS) == {"trial", "corporate", "other"}
assert set(HUMAN_STATES) == {"HUMAN_REQUESTED", "HUMAN_ACTIVE"}
