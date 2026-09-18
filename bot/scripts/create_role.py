"""Ставит пароль роли бота и пишет строку подключения в .env.

Пароль в терминал не печатается: транскрипт и логи это не место для секретов.

Как устроена строка подключения у Supabase (проверено по документации):
- Хост общего пулера выглядит как aws-N-регион.pooler.supabase.com, где N это
  индекс кластера, а не часть названия региона. Вывести его из проекта нельзя,
  его копируют из диалога Connect в панели.
- Имя пользователя на общем пулере для своей роли это РОЛЬ.PROJECT_REF,
  а не просто РОЛЬ.
- Сессионный режим это порт 5432 на хосте пулера. Он поддерживает подготовленные
  выражения, поэтому asyncpg работает без отключения кеша.
- Прямое соединение db.PROJECT_REF.supabase.co работает по IPv6, если не куплено
  дополнение IPv4. На обычном VPS это и есть причина брать пулер.

Запуск, строки берутся из .env (переменная окружения важнее файла):
    python scripts/create_role.py

Хост, порт и хвост имени пользователя берутся из POSTGRES_URL_TEMPLATE, а без
него из POSTGRES_URL_NON_POOLING. Если админская строка уже идёт через
сессионный пулер, шаблон не нужен. Если это прямое соединение Supabase, нужен:
хост пулера из прямой строки не выводится.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import secrets
import sys
from urllib.parse import quote, urlparse, urlunparse

import asyncpg
from dotenv import load_dotenv

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def put(env_text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, env_text, flags=re.MULTILINE):
        # Замена функцией: строку как шаблон re.sub разобрал бы по обратным слешам.
        return re.sub(pattern, lambda _: line, env_text, flags=re.MULTILINE)
    return env_text.rstrip("\n") + f"\n{line}\n"


def runtime_user(template_user: str | None, role: str) -> str:
    """На общем пулере Supabase имя это РОЛЬ.PROJECT_REF. Хвост берём из шаблона."""
    if template_user and "." in template_user:
        project_ref = template_user.split(".", 1)[1]
        return f"{role}.{project_ref}"
    return role


async def main() -> int:
    # interpolate=False: в пароле может встретиться $, его нельзя раскрывать.
    load_dotenv(ENV_PATH, override=False, interpolate=False)
    admin_url = os.environ.get("POSTGRES_URL_NON_POOLING")
    if not admin_url:
        print("нужен POSTGRES_URL_NON_POOLING (строка роли postgres)", file=sys.stderr)
        return 2

    role = os.environ.get("SCHOOL_DB_ROLE", "school_bot")
    if not SAFE_IDENT.match(role):
        print(f"SCHOOL_DB_ROLE={role!r}: только строчные буквы, цифры и подчёркивание", file=sys.stderr)
        return 2

    template_env = os.environ.get("POSTGRES_URL_TEMPLATE")
    template = template_env or admin_url
    admin_host = urlparse(admin_url).hostname or ""
    # Прямое соединение это db.REF.supabase.co, пулер это *.pooler.supabase.com.
    if admin_host.endswith(".supabase.co") and not template_env:
        print(
            "POSTGRES_URL_NON_POOLING смотрит на прямое соединение Supabase, нужен "
            "POSTGRES_URL_TEMPLATE: строка сессионного пулера из диалога Connect",
            file=sys.stderr,
        )
        return 2
    if urlparse(template).port == 6543:
        print(
            "в шаблоне порт 6543, это транзакционный пулер: asyncpg на нём ломается, "
            "нужна строка Session pooler с портом 5432",
            file=sys.stderr,
        )
        return 2

    password = secrets.token_urlsafe(32)
    conn = await asyncpg.connect(admin_url)
    try:
        # ALTER ROLE не принимает параметры, поэтому пароль экранируем сами.
        # token_urlsafe даёт только буквы, цифры, дефис и подчёркивание, но
        # удвоение кавычек оставляем: полагаться на алфавит генератора хрупко.
        literal = "'" + password.replace("'", "''") + "'"
        await conn.execute(f'alter role "{role}" with login password {literal}')
    finally:
        await conn.close()

    parts = urlparse(template)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = runtime_user(parts.username, role)
    runtime = urlunparse(parts._replace(netloc=f"{quote(user)}:{quote(password)}@{host}{port}"))

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    ENV_PATH.write_text(put(env_text, "POSTGRES_URL", runtime), encoding="utf-8")
    print(f"роль {role}: пароль обновлён, POSTGRES_URL записан в .env")
    print(f"пользователь в строке: {user}, хост: {host}{port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
