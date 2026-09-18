"""Маршрутизация входящих сообщений и запись переписки.

Три правила определяют всё остальное. Бот работает только в личных чатах.
Он различает ровно две роли: владелец школы по Telegram ID из окружения и
клиент. Клиенту бот пишет только в состоянии AI_ACTIVE. Текста сообщений в
логах нет, по правилу из CLAUDE.md: только идентификаторы, тип события и длины.

Путь одного update:
1. Заявка в processed_updates. Завершённый update пропускаем. Чужую
   незавершённую заявку ждём, пока её не завершат или она не протухнет.
2. Клиент, эпизод и входящее сообщение пишутся одной транзакцией.
3. Текстовый вопрос уходит в конвейер ответа (answer.py). /start и сообщения
   без текста модель не видят: на них у бота готовые реплики.
4. Ответ уходит в Telegram. Потом одной транзакцией пишутся ответ бота с
   источниками, событие о передаче руководителю, если она нужна, и
   завершение заявки.

Если база недоступна, клиент получает короткое извинение, а бот живёт дальше.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import texts
from .answer import AnswerService
from .config import Settings
from .db import DB_ERRORS
from .handoff import STATE_ACTIVE, STATE_REQUESTED, handoff_card, relay_card
from .leads import clean_lead_patch, is_qualified, owner_card
from .limits import ClientLimits, TokenBudget
from .memory import MemoryService
from .owner import OwnerHandler
from .repo import Repo
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

# Как часто проверять чужую незавершённую заявку на update.
CLAIM_POLL_SEC = 0.5

# Виды сообщений без текста. Вид пишется в meta, сам файл бот не скачивает.
NON_TEXT_KINDS = (
    "voice", "audio", "video_note", "video", "photo", "animation",
    "sticker", "document", "contact", "location", "poll",
)


def message_kind(message: dict[str, Any]) -> str:
    if message.get("text") is not None:
        return "text"
    for kind in NON_TEXT_KINDS:
        if kind in message:
            return kind
    return "other"


class UpdateHandler:
    def __init__(
        self,
        tg: TelegramClient,
        settings: Settings,
        repo: Repo,
        answers: AnswerService,
        memory: MemoryService,
        owner: OwnerHandler,
        limits: ClientLimits,
        budget: TokenBudget,
    ) -> None:
        self._tg = tg
        self._settings = settings
        self._repo = repo
        self._answers = answers
        self._memory = memory
        self._owner = owner
        self._limits = limits
        self._budget = budget

    @staticmethod
    def _message_of(update: dict[str, Any]) -> dict[str, Any] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        # В группах и каналах бот молчит: MVP работает один на один.
        if chat.get("type") != "private":
            return None
        sender = message.get("from") or {}
        if not sender.get("id") or sender.get("is_bot"):
            return None
        return message

    def chat_id_of(self, update: dict[str, Any]) -> int | None:
        """Ключ очереди. None означает, что update нас не касается."""
        message = self._message_of(update)
        if message is None:
            return None
        return int(message["chat"]["id"])

    async def handle(self, update: dict[str, Any]) -> None:
        message = self._message_of(update)
        if message is None:
            log.debug("update пропущен", extra={"update_id": update.get("update_id")})
            return

        update_id = int(update["update_id"])
        chat_id = int(message["chat"]["id"])
        user_id = int(message["from"]["id"])
        text = message.get("text")
        kind = message_kind(message)
        is_owner = user_id == self._settings.owner_telegram_id

        log.info(
            "входящее сообщение",
            extra={
                "update_id": update_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "role": "owner" if is_owner else "client",
                "kind": kind,
                "text_len": len(text) if text else 0,
            },
        )

        try:
            claimed = await self._claim(update_id)
        except DB_ERRORS as exc:
            self._log_db_error("заявка на update", exc, update_id)
            await self._send(chat_id, texts.client_temporary_problem(self._settings))
            return
        if not claimed:
            log.info("update уже обработан, пропускаем", extra={"update_id": update_id})
            return

        if is_owner:
            await self._handle_owner(update_id, message)
        else:
            await self._handle_client(update_id, message, text, kind)

    async def _claim(self, update_id: int) -> bool:
        """True: update наш. False: его уже обработали."""
        stale_sec = self._settings.update_claim_stale_sec
        while True:
            if await self._repo.claim_update(update_id, stale_sec):
                return True
            if await self._repo.update_completed(update_id):
                return False
            # Заявку держит другой обработчик. Так бывает при деплое: старый
            # контейнер ещё дорабатывает update, а новый уже получил его
            # повторно. Пропустить сразу нельзя: если старый упадёт, сообщение
            # останется без ответа. Ждём, пока заявку завершат или она протухнет.
            await asyncio.sleep(CLAIM_POLL_SEC)

    async def _handle_owner(self, update_id: int, message: dict[str, Any]) -> None:
        # Ответы клиентам и команды владельца живут в owner.py. Сами сообщения
        # владельца пишутся в тот разговор, которому адресованы.
        await self._owner.handle(message)
        await self._complete(update_id)

    async def _handle_client(
        self, update_id: int, message: dict[str, Any], text: str | None, kind: str
    ) -> None:
        s = self._settings
        chat_id = int(message["chat"]["id"])
        try:
            async with self._repo.tx() as conn:
                client = await self._repo.upsert_client(conn, message["from"])
                conversation, opened = await self._repo.resolve_conversation(
                    conn, client.id, s.episode_ttl_hours * 3600
                )
                client_message_id = await self._repo.insert_message(
                    conn,
                    conversation_id=conversation.id,
                    client_id=client.id,
                    role="client",
                    text=text,
                    telegram_message_id=message.get("message_id"),
                    meta={"kind": kind},
                )
                await self._repo.touch_conversation(conn, conversation.id)
        except DB_ERRORS as exc:
            self._log_db_error("запись входящего", exc, update_id)
            await self._send(chat_id, texts.client_temporary_problem(s))
            return

        if opened:
            log.info(
                "эпизод открыт",
                extra={"conversation_id": conversation.id, "client_id": client.id},
            )

        # Сообщение уже в базе как улика. Дальше три ограды: бан, лимиты по
        # числу сообщений и длина. Ни одна из них не зовёт модель и не
        # беспокоит владельца.
        if client.is_banned:
            log.info("клиент заблокирован, сообщение проигнорировано", extra={"client_id": client.id})
            await self._complete(update_id)
            return
        if not await self._within_limits(chat_id, client.id, conversation.id):
            await self._complete(update_id)
            return
        if self._limits.too_long(text):
            log.info("сообщение длиннее лимита", extra={"client_id": client.id, "text_len": len(text or "")})
            await self._send(chat_id, texts.client_too_long(s, s.client_max_message_chars))
            await self._complete(update_id)
            return

        if conversation.state == STATE_ACTIVE:
            # Владелец ведёт разговор сам: бот молчит, чтобы не говорить поверх
            # него, и только ретранслирует сообщения.
            log.info(
                "разговор ведёт владелец, ретранслирую",
                extra={"conversation_id": conversation.id, "state": conversation.state},
            )
            await self._relay_to_owner(message, conversation.id, client_message_id, text, kind)
            await self._complete(update_id)
            return

        # Состояние HUMAN_REQUESTED: переданный вопрос ждёт владельца, но на
        # новые вопросы бот отвечает. Иначе клиент сидит в тишине, пока владелец
        # занят, хотя половина вопросов есть в базе знаний.
        waiting_for_owner = conversation.state == STATE_REQUESTED

        reply, meta, handoff_reason = texts.client_non_text(s), {}, None
        profile_updates: dict[str, Any] = {}
        lead_from_model: dict[str, Any] | None = None
        if text is not None and text.startswith("/start"):
            reply = texts.client_greeting(s)
        elif text is not None and text.startswith("/human"):
            # Явная просьба: модель тут не нужна.
            reply, handoff_reason = texts.client_handoff(s), "command"
        elif text is not None and await self._budget_exhausted():
            # Денег на модель сегодня больше нет: вопрос уходит владельцу, как
            # любой другой, на который бот не может ответить.
            reply, handoff_reason = texts.client_handoff(s), "budget"
        elif text is not None:
            if opened:
                # Новый эпизод: обобщаем хвост прошлых разговоров, чтобы ответ
                # уже видел резюме. Это несколько секунд раз в сутки на клиента,
                # а у первого эпизода обобщать нечего и вызова не будет.
                await self._memory.update_summary_if_needed(
                    client.id, force=True, before_message_id=client_message_id
                )
            try:
                memory = await self._memory.load(
                    client.id, conversation.id, exclude_message_id=client_message_id
                )
                answer = await self._answers.answer(
                    text,
                    conversation_id=conversation.id,
                    message_id=client_message_id,
                    memory=memory,
                )
            except DB_ERRORS as exc:
                self._log_db_error("поиск по базе знаний", exc, update_id)
                await self._send(chat_id, texts.client_temporary_problem(s))
                return
            reply, meta, handoff_reason = answer.text, answer.meta, answer.handoff_reason
            profile_updates = self._memory.clean_updates(answer.profile_updates, memory.profile)
            lead_from_model = answer.lead

        sent = await self._send(chat_id, reply)
        if sent is None:
            await self._complete(update_id)
            return

        handoff_started = False
        try:
            async with self._repo.tx() as conn:
                await self._repo.insert_message(
                    conn,
                    conversation_id=conversation.id,
                    client_id=client.id,
                    role="bot",
                    text=reply,
                    telegram_message_id=sent.get("message_id"),
                    meta=meta,
                )
                if handoff_reason is not None:
                    handoff_started = await self._repo.start_handoff(
                        conn, conversation.id, handoff_reason
                    )
                    await self._repo.add_event(
                        "handoff_started" if handoff_started else "handoff_needed",
                        conversation_id=conversation.id,
                        client_id=client.id,
                        payload={"reason": handoff_reason},
                        conn=conn,
                    )
                await self._repo.update_profile(client.id, profile_updates, conn=conn)
                lead_state = await self._save_lead(
                    conn, client_id=client.id, conversation_id=conversation.id, lead=lead_from_model
                )
                await self._repo.touch_conversation(conn, conversation.id)
                await self._repo.complete_update(update_id, conn=conn)
        except DB_ERRORS as exc:
            # Ответ клиент уже получил. Потеряна только запись о нём.
            self._log_db_error("запись ответа", exc, update_id)
            return

        if handoff_started:
            await self._notify_handoff(message, conversation.id, client_message_id, handoff_reason)
        elif waiting_for_owner:
            # Владелец уже ждёт этот разговор: показываем ему и новый вопрос,
            # и то, что бот на него ответил. Второй карточки не шлём.
            await self._relay_to_owner(
                message, conversation.id, client_message_id, text, kind, bot_reply=reply
            )
        if lead_state is not None:
            await self._notify_owner(message["from"], conversation.id, lead_state)

        if profile_updates:
            log.info(
                "профиль дополнен",
                extra={"client_id": client.id, "fields": sorted(profile_updates)},
            )
        # Резюме считается после ответа: клиент его не ждёт.
        await self._memory.update_summary_if_needed(client.id)

    async def _within_limits(self, chat_id: int, client_id: int, conversation_id: int) -> bool:
        """False, если клиент превысил лимит. Пауза уходит один раз на окно."""
        try:
            rate = await self._limits.check(client_id)
        except DB_ERRORS as exc:
            # База лежит: следующий шаг всё равно упадёт на ней и извинится.
            self._log_db_error("проверка лимита", exc, None)
            return True
        if rate.allowed:
            return True
        log.warning(
            "лимит сообщений превышен",
            extra={"client_id": client_id, "window": rate.window, "count": rate.count},
        )
        if self._limits.should_warn(client_id, rate.window or "minute"):
            try:
                await self._repo.add_event(
                    "rate_limited",
                    conversation_id=conversation_id,
                    client_id=client_id,
                    payload={"window": rate.window, "count": rate.count},
                )
            except DB_ERRORS as exc:
                self._log_db_error("событие лимита", exc, None)
            await self._send(chat_id, texts.client_rate_limited(self._settings, rate.window or "minute"))
        return False

    async def _budget_exhausted(self) -> bool:
        try:
            exhausted, used = await self._budget.exhausted()
            if not exhausted:
                return False
            if await self._budget.alert_needed():
                await self._repo.add_event(
                    "budget_exhausted",
                    payload={"used": used, "limit": self._settings.llm_daily_token_budget},
                )
                log.error("дневной бюджет модели исчерпан", extra={"used": used})
                await self._send(
                    self._settings.owner_telegram_id,
                    texts.owner_budget_alert(self._settings, used, self._settings.llm_daily_token_budget),
                )
        except DB_ERRORS as exc:
            # Без базы бюджет не посчитать; лучше ответить, чем молчать.
            self._log_db_error("проверка бюджета", exc, None)
            return False
        return True

    async def _relay_to_owner(
        self,
        message: dict[str, Any],
        conversation_id: int,
        client_message_id: int,
        text: str | None,
        kind: str,
        bot_reply: str | None = None,
    ) -> None:
        """Сообщение клиента уходит владельцу, пока разговор у него.

        bot_reply заполнен, когда владелец ещё не подключился и бот ответил сам:
        владелец видит и вопрос, и ответ одним сообщением.
        """
        card = relay_card(
            client=message["from"], conversation_id=conversation_id, text=text, kind=kind,
            bot_reply=bot_reply,
        )
        sent = await self._send(self._settings.owner_telegram_id, card)
        if sent is None:
            log.error("ретрансляция владельцу не доставлена", extra={"conversation_id": conversation_id})
            return
        try:
            # Реплай владельца на это сообщение найдёт разговор.
            await self._repo.set_owner_relay(client_message_id, sent["message_id"])
        except DB_ERRORS as exc:
            self._log_db_error("отметка ретрансляции", exc, None)

    async def _notify_handoff(
        self, message: dict[str, Any], conversation_id: int, client_message_id: int, reason: str
    ) -> None:
        try:
            history = await self._repo.recent_messages(conversation_id, 5)
        except DB_ERRORS as exc:
            self._log_db_error("история для передачи", exc, None)
            history = []
        card = handoff_card(
            self._settings,
            client=message["from"],
            conversation_id=conversation_id,
            reason=reason,
            history=history,
        )
        sent = await self._send(self._settings.owner_telegram_id, card)
        if sent is None:
            log.error("уведомление о передаче не доставлено", extra={"conversation_id": conversation_id})
            return
        try:
            await self._repo.set_owner_relay(client_message_id, sent["message_id"])
        except DB_ERRORS as exc:
            self._log_db_error("отметка уведомления о передаче", exc, None)

    async def _save_lead(
        self,
        conn: Any,
        *,
        client_id: int,
        conversation_id: int,
        lead: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Заводит или дополняет заявку. Возвращает её и флаг «только что создана».

        Достаточность полей решает код: модель объявляет заявку готовой, стоит
        клиенту сказать «давайте».
        """
        if not lead:
            return None
        kind = lead.get("kind")
        if kind not in ("trial", "corporate", "other"):
            return None

        existing = await self._repo.active_lead(conn, client_id, kind)
        created = existing is None
        row = existing or await self._repo.create_lead(conn, client_id, conversation_id, kind)
        patch = clean_lead_patch(kind, lead, row)
        qualified = is_qualified(kind, {**row, **patch})
        if patch or qualified != row["is_qualified"]:
            row = await self._repo.update_lead(conn, row["id"], {**patch, "is_qualified": qualified})
        await self._repo.add_event(
            "lead_created" if created else "lead_updated",
            conversation_id=conversation_id,
            client_id=client_id,
            payload={"lead_id": row["id"], "kind": kind, "qualified": row["is_qualified"],
                     "fields": sorted(patch)},
            conn=conn,
        )
        log.info(
            "заявка создана" if created else "заявка дополнена",
            extra={"lead_id": row["id"], "kind": kind, "qualified": row["is_qualified"],
                   "fields": sorted(patch)},
        )
        return row

    async def _notify_owner(
        self, sender: dict[str, Any], conversation_id: int, lead: dict[str, Any]
    ) -> None:
        """Два уведомления на заявку: о новой и о собранной. Уточнения молчат.

        Отметка ставится только после успешной отправки, поэтому не дошедшее
        уведомление уйдёт при следующем изменении заявки.
        """
        if lead["notified_at"] is None:
            column = "notified_at"
        elif lead["is_qualified"] and lead["qualified_notified_at"] is None:
            column = "qualified_notified_at"
        else:
            return

        card = owner_card(
            self._settings,
            client=sender,
            kind=lead["kind"],
            lead=lead,
            conversation_id=conversation_id,
            qualified=lead["is_qualified"],
        )
        sent = await self._send(self._settings.owner_telegram_id, card)
        if sent is None:
            # Заявка уже в базе, отметки нет: уведомление уйдёт при следующем
            # изменении заявки.
            log.error("уведомление владельцу не доставлено", extra={"lead_id": lead["id"]})
            return
        try:
            await self._repo.mark_lead_notified(lead["id"], column)
            if column == "notified_at" and lead["is_qualified"]:
                # Заявка пришла сразу полной: второе сообщение не нужно.
                await self._repo.mark_lead_notified(lead["id"], "qualified_notified_at")
            await self._repo.add_event(
                "owner_notified",
                conversation_id=conversation_id,
                client_id=lead["client_id"],
                payload={"lead_id": lead["id"], "kind": lead["kind"], "qualified": lead["is_qualified"]},
            )
        except DB_ERRORS as exc:
            # Хуже всего повторное уведомление, а не потерянная отметка.
            self._log_db_error("отметка уведомления", exc, None)

    async def _complete(self, update_id: int) -> None:
        try:
            await self._repo.complete_update(update_id)
        except DB_ERRORS as exc:
            # Заявка протухнет сама. Повторный ответ возможен, только если
            # Telegram передоставит update, а это бывает лишь после падения.
            self._log_db_error("завершение update", exc, update_id)

    async def _send(self, chat_id: int, text: str) -> dict[str, Any] | None:
        """Результат sendMessage или None, если отправить не удалось."""
        try:
            return await self._tg.send_message(chat_id, text)
        except TelegramError as exc:
            if exc.is_forbidden:
                log.info("клиент заблокировал бота", extra={"chat_id": chat_id})
                await self._mark_blocked(chat_id)
                return None
            log.error(
                "не удалось отправить ответ",
                extra={"chat_id": chat_id, "code": exc.code, "description": exc.description},
            )
        except Exception as exc:
            log.error(
                "сбой отправки ответа",
                extra={"chat_id": chat_id, "error": type(exc).__name__},
            )
        return None

    async def _mark_blocked(self, chat_id: int) -> None:
        # В личном чате chat_id совпадает с id пользователя.
        try:
            await self._repo.mark_blocked(chat_id)
        except DB_ERRORS as exc:
            self._log_db_error("флаг блокировки", exc, None)

    @staticmethod
    def _log_db_error(step: str, exc: BaseException, update_id: int | None) -> None:
        # Только тип ошибки: текст исключения asyncpg может нести куски данных.
        log.error(
            "база недоступна",
            extra={"step": step, "update_id": update_id, "error": type(exc).__name__},
        )
