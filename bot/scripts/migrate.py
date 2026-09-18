"""Применятель миграций: обычные SQL-файлы и учёт применённого в самой базе.

Alembic сюда не тащим: он будет драться с руками написанными ролями и
политиками RLS, а файлов у нас десяток.

Строка роли postgres берётся из POSTGRES_URL_NON_POOLING в .env, так она не
попадает ни в историю терминала, ни в транскрипт. Переменная окружения с тем же
именем важнее файла. Запуск только с ноутбука:
    python scripts/migrate.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
from typing import Callable

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
ENV_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def render(sql: str, schema: str, role: str, embedding_dim: int | None = None) -> str:
    sql = sql.replace("{{schema}}", schema).replace("{{role}}", role)
    if "{{embedding_dim}}" in sql:
        if embedding_dim is None:
            raise ValueError("миграции нужна размерность вектора: задайте EMBEDDING_DIM")
        sql = sql.replace("{{embedding_dim}}", str(embedding_dim))
    return sql


async def apply_migrations(
    conn: asyncpg.Connection,
    schema: str,
    role: str,
    embedding_dim: int | None = None,
    report: Callable[[str], None] = print,
) -> list[str]:
    """Применяет неприменённые миграции и возвращает их версии.

    Тесты зовут эту же функцию для схемы school_test, так что схема в тестах и
    в продакшене собирается одним кодом.
    """
    for name, value in (("SCHOOL_SCHEMA", schema), ("SCHOOL_DB_ROLE", role)):
        if not SAFE_IDENT.match(value):
            raise ValueError(f"{name}={value!r}: только строчные буквы, цифры и подчёркивание")
    if embedding_dim is not None and not 1 <= embedding_dim <= 2000:
        # HNSW в pgvector строится по vector размерностью не больше 2000.
        raise ValueError(f"EMBEDDING_DIM={embedding_dim}: нужно от 1 до 2000")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError("миграций не найдено")

    await conn.execute(f'create schema if not exists "{schema}"')
    await conn.execute(
        f'create table if not exists "{schema}".schema_migrations ('
        "  version text primary key,"
        "  applied_at timestamptz not null default now())"
    )
    applied = {
        r["version"]
        for r in await conn.fetch(f'select version from "{schema}".schema_migrations')
    }

    applied_now: list[str] = []
    for path in files:
        version = path.stem
        if version in applied:
            report(f"пропуск  {version}")
            continue
        sql = render(path.read_text(encoding="utf-8"), schema, role, embedding_dim)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                f'insert into "{schema}".schema_migrations (version) values ($1)', version
            )
        applied_now.append(version)
        report(f"применена {version}")
    return applied_now


async def main() -> int:
    # interpolate=False: в пароле может встретиться $, его нельзя раскрывать.
    load_dotenv(ENV_PATH, override=False, interpolate=False)
    url = os.environ.get("POSTGRES_URL_NON_POOLING")
    if not url:
        print("нужен POSTGRES_URL_NON_POOLING (строка роли postgres)", file=sys.stderr)
        return 2

    schema = os.environ.get("SCHOOL_SCHEMA", "school")
    role = os.environ.get("SCHOOL_DB_ROLE", "school_bot")
    raw_dim = os.environ.get("EMBEDDING_DIM")
    try:
        embedding_dim = int(raw_dim) if raw_dim else None
    except ValueError:
        print(f"EMBEDDING_DIM={raw_dim!r}: нужно целое число", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(url)
    try:
        await apply_migrations(conn, schema, role, embedding_dim)
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        await conn.close()

    print(f"схема {schema}, роль {role}: готово")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
