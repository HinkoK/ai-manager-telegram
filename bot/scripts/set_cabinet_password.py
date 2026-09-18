"""Логин и пароль кабинета. Пароль в .env попадает только хешем.

Запуск из папки bot:
    .venv/bin/python scripts/set_cabinet_password.py          # логин owner
    .venv/bin/python scripts/set_cabinet_password.py admin    # другой логин

Пароль вводится скрыто и в терминале не отображается. В .env пишутся
CABINET_LOGIN и CABINET_PASSWORD_HASH, самого пароля там нет: если файл утечёт,
войти по нему не получится.
"""

from __future__ import annotations

import getpass
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402

ENV_PATH = pathlib.Path(__file__).resolve().parents[2] / ".env"
MIN_LENGTH = 10


def put(env_text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, env_text, flags=re.MULTILINE):
        return re.sub(pattern, lambda _: line, env_text, flags=re.MULTILINE)
    return env_text.rstrip("\n") + f"\n{line}\n"


def main() -> int:
    login = sys.argv[1] if len(sys.argv) > 1 else "owner"
    password = getpass.getpass("Пароль кабинета: ")
    if len(password) < MIN_LENGTH:
        print(f"пароль короче {MIN_LENGTH} символов, так не пойдёт", file=sys.stderr)
        return 2
    if password != getpass.getpass("Ещё раз: "):
        print("пароли не совпали", file=sys.stderr)
        return 2

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    env_text = put(env_text, "CABINET_LOGIN", login)
    env_text = put(env_text, "CABINET_PASSWORD_HASH", hash_password(password))
    ENV_PATH.write_text(env_text, encoding="utf-8")
    print(f"логин {login}: хеш пароля записан в .env. Перезапустите бота.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
