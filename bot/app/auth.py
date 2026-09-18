"""Вход в кабинет: хеш пароля и токен сессии.

Пароль хранится в .env хешем scrypt, а не текстом. scrypt взят из стандартной
библиотеки: лишняя зависимость ради хеша не нужна, а по стойкости он для этой
задачи не хуже bcrypt.

Формат строки: scrypt:n:r:p:соль:хеш, соль и хеш в base64. Так в .env лежит
всё нужное для проверки, и параметры можно поднять, не ломая старые пароли.

Разделитель двоеточие, а не доллар, как принято у passlib: docker compose
подставляет переменные в значения .env, и $16384 из формата scrypt$... в
контейнере превращался в пустоту. Старый формат с долларом читается
по-прежнему, чтобы уже записанные пароли не ломались.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32
TOKEN_BYTES = 32


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DK_LEN)
    return ":".join(
        ["scrypt", str(n), str(r), str(p), base64.b64encode(salt).decode(), base64.b64encode(dk).decode()]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        separator = ":" if ":" in stored else "$"
        algorithm, n, r, p, salt_b64, hash_b64 = stored.split(separator)
        if algorithm != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    # Сравнение постоянного времени: иначе по задержке подбирают хеш побайтно.
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """В базе лежит хеш токена, как и для пароля: утёкшая база не даёт войти."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
