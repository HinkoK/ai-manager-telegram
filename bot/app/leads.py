"""Заявки: какие поля бывают, когда данных достаточно и что видит владелец.

Поля не выдуманы, они взяты из документов школы. trial.md просит удобные дни
и время с часовым поясом, цель и примерный уровень, причём уровень прямо
назван необязательным. corporate.md просит число людей, сферу, цель и время.

Решает достаточность код, а не модель: модели свойственно объявлять заявку
готовой, когда клиент сказал «давайте».
"""

from __future__ import annotations

from typing import Any

from .config import Settings

KINDS = ("trial", "corporate", "other")

# Что вообще может быть у заявки каждого вида. Всё остальное отбрасывается.
FIELDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "trial": ("goal", "level", "format", "timezone", "preferred_time"),
    "corporate": ("goal", "company", "team_size", "sphere", "timezone", "preferred_time", "format"),
    "other": ("goal", "level", "format", "timezone", "preferred_time", "notes"),
}

# Без чего менеджер не сможет предложить окна.
REQUIRED_BY_KIND: dict[str, tuple[str, ...]] = {
    "trial": ("goal", "preferred_time"),
    "corporate": ("team_size", "sphere", "goal", "preferred_time"),
    "other": (),
}

KIND_NAMES = {"trial": "пробный урок", "corporate": "корпоративное обучение", "other": "заявка"}

FIELD_NAMES = {
    "goal": "цель", "level": "уровень", "format": "формат", "timezone": "часовой пояс",
    "preferred_time": "удобное время", "company": "компания", "team_size": "человек в команде",
    "sphere": "сфера", "notes": "заметка",
}

TEXT_VALUE_MAX = 300


def clean_lead_patch(kind: str, raw: dict[str, Any], known: dict[str, Any]) -> dict[str, Any]:
    """Оставляет поля, которые бывают у этого вида заявки, непустые и новые."""
    patch: dict[str, Any] = {}
    for field in FIELDS_BY_KIND.get(kind, ()):
        value = raw.get(field)
        if value is None:
            continue
        if field == "team_size":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if not 1 <= value <= 1000:
                continue
        elif isinstance(value, str):
            value = value.strip()[:TEXT_VALUE_MAX]
            if not value:
                continue
        else:
            continue
        if known.get(field) == value:
            continue
        patch[field] = value
    return patch


def missing_fields(kind: str, lead: dict[str, Any]) -> list[str]:
    return [f for f in REQUIRED_BY_KIND.get(kind, ()) if not lead.get(f)]


def is_qualified(kind: str, lead: dict[str, Any]) -> bool:
    # Часовой пояс обязателен вместе со временем: «по вечерам» без пояса
    # менеджеру ничего не даёт.
    if missing_fields(kind, lead):
        return False
    if lead.get("preferred_time") and not lead.get("timezone"):
        return False
    return True


def client_title(client: dict[str, Any]) -> str:
    name = client.get("first_name") or "Без имени"
    username = client.get("username")
    return f"{name} (@{username})" if username else name


def owner_card(
    settings: Settings,
    *,
    client: dict[str, Any],
    kind: str,
    lead: dict[str, Any],
    conversation_id: int,
    qualified: bool,
) -> str:
    head = "Заявка собрана" if qualified else "Новая заявка"
    lines = [f"{head}: {KIND_NAMES.get(kind, kind)}", client_title(client), ""]
    for field in FIELDS_BY_KIND.get(kind, ()):
        value = lead.get(field)
        if value not in (None, ""):
            lines.append(f"{FIELD_NAMES.get(field, field)}: {value}")
    if not qualified:
        missing = missing_fields(kind, lead)
        if lead.get("preferred_time") and not lead.get("timezone"):
            missing.append("timezone")
        lines.append("")
        lines.append("Не хватает: " + ", ".join(FIELD_NAMES.get(f, f) for f in missing))
        lines.append("Бот дособирает сам, отдельное сообщение придёт, когда заявка будет полной.")
    lines.append("")
    if settings.cabinet_url:
        lines.append(f"{settings.cabinet_url.rstrip('/')}/conversations/{conversation_id}")
    else:
        lines.append(f"Разговор №{conversation_id}. Кабинет появится на этапе 7.")
    return "\n".join(lines)
