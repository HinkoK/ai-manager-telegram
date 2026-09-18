"""Передача разговора владельцу: состояния, причины и тексты для владельца.

Состояний два. HUMAN_REQUESTED значит «бот передал, владелец ещё не отвечал»:
такие разговоры торопит фоновая задача. HUMAN_ACTIVE значит «владелец уже
ведёт разговор сам».

Ответ владельца находит свой разговор по owner_relay_message_id: это id
сообщения в чате владельца, на которое он отвечает реплаем.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .leads import client_title

STATE_AI = "AI_ACTIVE"
STATE_REQUESTED = "HUMAN_REQUESTED"
STATE_ACTIVE = "HUMAN_ACTIVE"
HUMAN_STATES = (STATE_REQUESTED, STATE_ACTIVE)

# Причины передачи. Ключи уже приходят из конвейера ответа с этапа 3.
REASON_NAMES = {
    "not_in_knowledge": "нет ответа в базе знаний",
    "client_request": "клиент просит живого человека",
    "complaint": "жалоба или недовольство",
    "no_sources": "модель ответила без источников",
    "llm_error": "сбой модели",
    "invalid_json": "модель ответила неразборчиво",
    "unspecified": "модель не назвала причину",
    "command": "клиент написал /human",
    "budget": "дневной бюджет модели исчерпан",
}

ROLE_PREFIX = {"client": "Клиент", "bot": "Бот", "owner": "Вы", "system": "Система"}


def reason_name(reason: str | None) -> str:
    return REASON_NAMES.get(reason or "", reason or "без причины")


def _tail(settings: Settings, conversation_id: int) -> str:
    if settings.cabinet_url:
        return f"{settings.cabinet_url.rstrip('/')}/conversations/{conversation_id}"
    return f"Разговор №{conversation_id}. Кабинет появится на этапе 7."


def handoff_card(
    settings: Settings,
    *,
    client: dict[str, Any],
    conversation_id: int,
    reason: str | None,
    history: list[tuple[str, str]],
) -> str:
    lines = [
        f"Нужен ваш ответ: {reason_name(reason)}",
        client_title(client),
        "",
    ]
    for role, text in history:
        lines.append(f"{ROLE_PREFIX.get(role, role)}: {text}")
    lines += [
        "",
        "Ответьте реплаем на это сообщение, и я передам текст клиенту.",
        "Пока вы не ответили, на другие вопросы клиента бот отвечает сам, "
        "а этот вопрос остаётся за вами.",
        f"Вернуть разговор боту: /bot реплаем. Рабочее время {settings.owner_hours}.",
        _tail(settings, conversation_id),
    ]
    return "\n".join(lines)


def relay_card(
    *,
    client: dict[str, Any],
    conversation_id: int,
    text: str | None,
    kind: str,
    bot_reply: str | None = None,
) -> str:
    """Сообщение клиента, пока разговор у владельца.

    Пока владелец не подключился, бот продолжает отвечать на новые вопросы,
    и его ответ идёт в том же сообщении: иначе владелец не поймёт, на что
    клиент уже получил ответ.
    """
    body = text if text is not None else f"[{kind}, бот принимает только текст]"
    lines = [f"Разговор №{conversation_id}, {client_title(client)}:", body]
    if bot_reply:
        lines += ["", f"Бот ответил сам: {bot_reply}"]
    return "\n".join(lines)


def reminder_card(settings: Settings, *, client: dict[str, Any], conversation_id: int,
                  reason: str | None, hours: float) -> str:
    return (
        f"Клиент ждёт ответа больше {hours:g} ч: {client_title(client)}\n"
        f"Причина передачи: {reason_name(reason)}\n"
        "Ответьте реплаем на уведомление о передаче.\n"
        f"{_tail(settings, conversation_id)}"
    )


def returned_card(settings: Settings, *, client: dict[str, Any], conversation_id: int,
                  hours: float) -> str:
    return (
        f"Разговор вернулся боту: за {hours:g} ч ответа не было.\n"
        f"{client_title(client)}\n"
        "Клиенту об этом не сообщал, он помечен как требующий внимания.\n"
        f"{_tail(settings, conversation_id)}"
    )


def poller_silent_card(minutes: float) -> str:
    return (
        f"Telegram не отвечает боту больше {minutes:g} мин.\n"
        "Сообщения клиентов сейчас не приходят. Обычно это сбой у Telegram или "
        "сети на сервере: перезапуск контейнера не поможет.\n"
        "Как только связь вернётся, я напишу."
    )


def poller_recovered_card() -> str:
    return "Связь с Telegram восстановилась, сообщения снова приходят."


def status_card(*, human: int, requested: int, new_leads: int) -> str:
    return (
        f"Разговоров у вас: {human} (ждут первого ответа: {requested})\n"
        f"Новых заявок: {new_leads}"
    )
