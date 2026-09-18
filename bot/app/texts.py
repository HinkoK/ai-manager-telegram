"""Все реплики бота в одном месте.

Ответы по существу пишет модель. Здесь то, что бот говорит сам: приветствие,
отказ на голосовые, передача руководителю, извинение при сбое.
"""

from __future__ import annotations

from .config import Settings


def client_greeting(s: Settings) -> str:
    return (
        f"Здравствуйте! Это менеджер школы английского {s.school_name}.\n\n"
        "Отвечу на вопросы про курсы, цены, расписание и преподавателей, помогу "
        "выбрать формат и записаться на бесплатный пробный урок 30 минут.\n\n"
        "С чего начнём?"
    )


def client_handoff(s: Settings) -> str:
    """Ответа в материалах нет, модель не ответила или клиент просит человека.

    Текст фиксированный, а не от модели: модель в этом месте любит обещать
    лишнее. Сама пересылка владельцу появится на этапе 6, до этапа 9 бот не
    опубликован, так что обещание никого не подведёт.
    """
    return (
        "Уточню это у руководителя школы и вернусь с ответом в этом чате. "
        f"Руководитель на связи {s.owner_hours}."
    )


def client_non_text(s: Settings) -> str:
    return (
        "Я понимаю только текст. Голосовые, фото и файлы не читаю, напишите "
        "вопрос сообщением, пожалуйста."
    )


def owner_greeting(s: Settings) -> str:
    return (
        f"Вы владелец школы {s.school_name}, узнал вас по Telegram ID.\n\n"
        "Сюда будут приходить уведомления о заявках и о разговорах, которые бот "
        "передаст вам. Команды владельца появятся на этапе передачи человеку."
    )


def owner_non_text(s: Settings) -> str:
    return "Понимаю только текст: ответьте клиенту сообщением."


def owner_unknown_command(s: Settings) -> str:
    return (
        "Команды: /status покажет счётчики, /bot реплаем вернёт разговор боту, "
        "/ban и /unban реплаем заблокируют и разблокируют клиента."
    )


def owner_no_target(s: Settings, reason: str) -> str:
    if reason == "none":
        return "Сейчас ни один разговор не передан вам, отвечать некому."
    if reason == "many":
        return (
            "У вас несколько разговоров. Ответьте реплаем на нужное уведомление, "
            "иначе я не пойму, кому писать."
        )
    return "Не нашёл разговор по этому сообщению. Ответьте реплаем на уведомление о передаче."


def owner_delivered(s: Settings, conversation_id: int) -> str:
    return f"Отправил клиенту (разговор №{conversation_id})."


def owner_not_delivered(s: Settings, blocked: bool) -> str:
    if blocked:
        return "Клиент заблокировал бота, сообщение не доставлено."
    return "Telegram не принял сообщение, попробуйте ещё раз."


def owner_returned(s: Settings, conversation_id: int, changed: bool) -> str:
    if changed:
        return f"Разговор №{conversation_id} снова у бота. Клиенту я об этом не писал."
    return f"Разговор №{conversation_id} и так был у бота."


def owner_temporary_problem(s: Settings) -> str:
    return "Технический сбой на моей стороне, повторите через минуту."


def client_rate_limited(s: Settings, window: str) -> str:
    if window == "day":
        return (
            "На сегодня сообщений уже много, давайте продолжим завтра. Если вопрос "
            f"срочный, руководитель на связи {s.owner_hours}."
        )
    return "Слишком много сообщений подряд. Подождите минуту, и я отвечу."


def client_too_long(s: Settings, limit: int) -> str:
    return (
        f"Сообщение получилось длиннее {limit} символов, я такие не читаю. "
        "Напишите, пожалуйста, короче или разбейте на несколько."
    )


def owner_budget_alert(s: Settings, used: int, limit: int) -> str:
    return (
        f"Дневной бюджет модели исчерпан: {used:,} из {limit:,} токенов за сутки.\n"
        "Бот перестал отвечать по базе знаний и передаёт разговоры вам. "
        "Лимит освободится сам по мере того, как старые вызовы выйдут из суток, "
        "или поднимите LLM_DAILY_TOKEN_BUDGET в .env."
    ).replace(",", " ")


def owner_ban_result(s: Settings, conversation_id: int, banned: bool, changed: bool) -> str:
    if not changed:
        return f"Клиент из разговора №{conversation_id} не найден."
    if banned:
        return f"Клиент из разговора №{conversation_id} заблокирован: бот его сообщения игнорирует."
    return f"Клиент из разговора №{conversation_id} разблокирован."


def client_temporary_problem(s: Settings) -> str:
    """База недоступна. Молчать нельзя: клиент решит, что бот мёртв."""
    return (
        "Извините, у меня технический сбой, сообщение могло не сохраниться. "
        "Напишите, пожалуйста, ещё раз через минуту."
    )
