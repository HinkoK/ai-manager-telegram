"""Все настройки приходят из окружения. Секреты в коде не живут."""

from __future__ import annotations

from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: str
    # Владелец школы. Сообщения с этого id маршрутизируются как «владелец»,
    # всё остальное как «клиент». Один владелец на MVP, по плану.
    owner_telegram_id: int
    # Переопределяется в тестах, чтобы не ходить в живой Telegram.
    telegram_api_root: str = "https://api.telegram.org"
    # Сколько Telegram держит long poll открытым. Read-таймаут HTTP считается от него.
    poll_timeout_sec: int = 30

    # --- Процесс ------------------------------------------------------------
    log_level: str = "INFO"
    # Сколько секунд ждать завершения уже начатых обработчиков при остановке.
    shutdown_drain_sec: float = 20.0
    # Через сколько секунд простоя убирать воркер чата, чтобы словарь не рос.
    chat_worker_idle_sec: float = 300.0

    # --- База ---------------------------------------------------------------
    # Рантайм: роль бота, у Supabase это пулер сессионного режима (порт 5432).
    # Транзакционный пулер (6543) ломает подготовленные выражения asyncpg.
    postgres_url: str
    # Схема бота. Тесты гоняются в school_test и настоящих данных не видят.
    school_schema: str = "school"
    db_pool_min: int = 1
    db_pool_max: int = 5
    # Через сколько тишины эпизод считается законченным.
    episode_ttl_hours: float = 24.0
    # Заявка на update без завершения старше этого считается брошенной.
    update_claim_stale_sec: float = 120.0
    # Сколько держим отметки об обработанных update. Чистятся при старте.
    processed_updates_ttl_days: int = 7

    # --- Модель -------------------------------------------------------------
    # Любой OpenAI-совместимый API. Выбран OpenRouter, модель GPT-5.6 Luna.
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str
    llm_model: str
    # Размышление модели. none выключает его: ответ быстрее, а токены
    # размышления OpenRouter считает выходными и берёт за них деньги.
    # Пусто: поле не передаётся, решает провайдер.
    llm_reasoning_effort: ReasoningEffort | None = None
    # Поле provider запроса OpenRouter, JSON как есть: маршрут, отказ от
    # провайдеров, собирающих данные. Другие API это поле не знают.
    llm_provider: dict[str, Any] | None = None
    llm_timeout_sec: float = 45.0
    llm_max_tokens: int = 1200

    # Эмбеддинги. Адрес и ключ по умолчанию те же, что у модели.
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str
    # Размерность вектора зашита в миграцию 0003. Смена модели или размерности
    # это новая миграция и полная перезагрузка базы знаний.
    embedding_dim: int
    embedding_provider: dict[str, Any] | None = None

    # --- Лимиты и бюджет ------------------------------------------------------
    # Сообщений от одного клиента. 0 выключает лимит.
    client_msgs_per_minute: int = 20
    client_msgs_per_day: int = 200
    # Длиннее этого в модель не идёт: клиенту просьба написать короче.
    client_max_message_chars: int = 2000
    # Токенов модели за скользящие сутки, вход плюс выход. 0 выключает бюджет.
    # 2 миллиона это примерно 500 сообщений с памятью, около $0.6 на Luna.
    llm_daily_token_budget: int = 2_000_000
    # Сколько подряд неверных паролей блокируют вход в кабинет и на сколько.
    cabinet_login_attempts: int = 10
    cabinet_login_block_minutes: float = 15.0
    # Тело запроса к API больше этого отклоняется до разбора.
    api_max_body_bytes: int = 65_536

    # --- Кабинет владельца ----------------------------------------------------
    # Логин и хеш пароля ставит scripts/set_cabinet_password.py. Пока хеша нет,
    # кабинет отвечает 503 и войти нельзя.
    cabinet_login: str = "owner"
    cabinet_password_hash: str | None = None
    cabinet_session_days: int = 7
    # Secure-cookie требует https. Для локальной разработки по http выключить.
    cabinet_cookie_secure: bool = True

    # --- Передача владельцу ---------------------------------------------------
    # Через сколько часов молчания владельцу уходит напоминание (один раз).
    handoff_reminder_hours: float = 2.0
    # Через сколько часов разговор возвращается боту сам, с пометкой
    # «требует внимания». Клиенту об этом не сообщаем.
    handoff_return_hours: float = 24.0
    # Как часто фоновая задача проверяет просроченные разговоры.
    jobs_interval_sec: float = 60.0
    # Если Telegram не отвечает дольше этого, владельцу уходит алерт один раз.
    # Контейнер перезапускать бесполезно: проблема снаружи.
    poller_silence_minutes: float = 10.0

    # --- Память -------------------------------------------------------------
    # Сколько последних сообщений эпизода уходит модели. Всё, что старше,
    # живёт в резюме.
    history_messages: int = 15
    # Резюме пересобирается, когда после прошлого накопилось столько сообщений,
    # и в начале нового эпизода, если остался необобщённый хвост.
    summary_every_messages: int = 10
    summary_max_chars: int = 1200
    # Перед поиском вопрос переписывается в самостоятельный запрос с учётом
    # истории и профиля. Выключить: искать по сырому сообщению клиента.
    rewrite_query: bool = True

    # --- Поиск по базе знаний -----------------------------------------------
    search_top_k: int = 8
    # Куски с косинусной близостью ниже порога модели не показываем. Порог
    # зависит от модели эмбеддингов и подбирается на eval.
    search_min_score: float = 0.2
    # Папка с базой знаний. Пусто: knowledge/ в корне репозитория.
    knowledge_dir: str | None = None

    # --- Школа --------------------------------------------------------------
    school_name: str = "Bridge English"
    owner_hours: str = "9:00-21:00 МСК"
    # Адрес кабинета владельца. Пусто: в карточке заявки будет только id
    # разговора. Кабинет появится на этапе 7.
    cabinet_url: str | None = None

    @property
    def embedding_url(self) -> str:
        return self.embedding_base_url or self.llm_base_url

    @property
    def embedding_key(self) -> str:
        return self.embedding_api_key or self.llm_api_key


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
