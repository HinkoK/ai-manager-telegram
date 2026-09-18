"""Структурные логи одной строкой JSON и вырезание секретов из вывода."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


class SecretRedactor(logging.Filter):
    """Токен бота не должен попасть ни в один лог, даже внутри текста ошибки httpx."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._redact_value(a) for a in record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = self._redact(value)
        return True

    def _redact_value(self, value: Any) -> Any:
        return self._redact(value) if isinstance(value, str) else value

    def redact(self, text: str) -> str:
        return self._redact(text)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # Находка аудита: трассировка собирается уже после фильтра, а httpx
            # пишет в текст ошибки адрес запроса вместе с токеном бота.
            payload["exc"] = self._redactor.redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    redactor = SecretRedactor(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(redactor))
    handler.addFilter(redactor)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn ставит свои обработчики, снимаем их, чтобы не было двойных строк.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("httpx").setLevel("WARNING")
