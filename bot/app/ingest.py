"""Загрузка базы знаний: knowledge/*.md -> куски -> эмбеддинги -> Postgres.

Запуск из папки bot, настройки берутся из .env в корне репозитория:
    .venv/bin/python -m app.ingest

Идемпотентно. Файл с тем же хешем и той же моделью эмбеддингов пропускается.
Изменённый файл пересобирается целиком одной транзакцией. Файл, которого больше
нет в папке, удаляется вместе с кусками. Сменилась модель эмбеддингов, значит
перезагружается всё.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Database
from .knowledge import Document, default_knowledge_dir, load_documents
from .llm import LLMError, OpenAICompatClient
from .logging_setup import setup_logging
from .repo import Repo

ROOT_ENV = pathlib.Path(__file__).resolve().parents[2] / ".env"


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    chunks: int = 0


async def ingest(
    repo: Repo,
    embedder: OpenAICompatClient,
    documents: list[Document],
    *,
    model: str,
    dim: int,
    provider: dict[str, Any] | None = None,
) -> IngestReport:
    if not documents:
        # Пустая папка иначе стёрла бы всю базу знаний.
        raise ValueError("в папке нет ни одного файла базы знаний, удалять всё не буду")

    stored = await repo.list_documents()
    report = IngestReport()
    for document in documents:
        old = stored.get(document.path)
        if old and old.content_hash == document.content_hash and old.embedding_model == model:
            report.skipped.append(document.path)
            continue

        texts = [chunk.content for chunk in document.chunks]
        vectors: list[list[float]] = []
        if texts:
            try:
                result = await embedder.embed(model=model, texts=texts, dim=dim, provider=provider)
            except LLMError as exc:
                await repo.add_llm_call(purpose="embed", model=model, latency_ms=exc.latency_ms, error=exc.kind)
                raise
            await repo.add_llm_call(
                purpose="embed", model=model, latency_ms=result.latency_ms,
                prompt_tokens=result.prompt_tokens,
            )
            vectors = result.vectors

        await repo.replace_document(
            path=document.path,
            title=document.title,
            content_hash=document.content_hash,
            embedding_model=model,
            chunks=[
                (chunk.index, chunk.section_title, chunk.content, vector)
                for chunk, vector in zip(document.chunks, vectors)
            ],
        )
        (report.updated if old else report.added).append(document.path)
        report.chunks += len(document.chunks)

    present = {document.path for document in documents}
    for path in sorted(stored):
        if path not in present:
            await repo.delete_document(path)
            report.deleted.append(path)
    return report


async def check_knowledge_schema(repo: Repo, settings: Settings) -> dict[str, int]:
    """Бот с базой другой размерности или другой модели ищет мусор. Лучше не стартовать."""
    dim = await repo.embedding_column_dim()
    if dim is None:
        raise RuntimeError("нет таблицы knowledge_chunks: примените миграции scripts/migrate.py")
    if dim != settings.embedding_dim:
        raise RuntimeError(
            f"колонка эмбеддингов размерности {dim}, а EMBEDDING_DIM={settings.embedding_dim}: "
            "размерность меняется только новой миграцией"
        )
    models = await repo.knowledge_models()
    foreign = sorted(set(models) - {settings.embedding_model})
    if foreign:
        raise RuntimeError(
            f"база знаний загружена моделью {', '.join(foreign)}, а EMBEDDING_MODEL="
            f"{settings.embedding_model}: перезагрузите базу, python -m app.ingest"
        )
    return models


async def main() -> int:
    settings = Settings(_env_file=str(ROOT_ENV) if ROOT_ENV.exists() else None)
    setup_logging("WARNING", secrets=[settings.telegram_bot_token, settings.llm_api_key, settings.embedding_key])
    directory = pathlib.Path(settings.knowledge_dir) if settings.knowledge_dir else default_knowledge_dir()
    documents = load_documents(directory)
    print(f"папка {directory}: файлов {len(documents)}, кусков {sum(len(d.chunks) for d in documents)}")

    db = Database(settings.postgres_url, settings.school_schema, min_size=1, max_size=2)
    embedder = OpenAICompatClient(settings.embedding_url, settings.embedding_key, settings.llm_timeout_sec)
    await db.connect()
    repo = Repo(db)
    try:
        dim = await repo.embedding_column_dim()
        if dim != settings.embedding_dim:
            print(f"колонка эмбеддингов {dim}, EMBEDDING_DIM={settings.embedding_dim}: сначала миграции", file=sys.stderr)
            return 2
        report = await ingest(
            repo, embedder, documents,
            model=settings.embedding_model, dim=settings.embedding_dim,
            provider=settings.embedding_provider,
        )
    except LLMError as exc:
        print(f"эмбеддинги не получены: {exc.kind}. Уже загруженное сохранено, повторный запуск продолжит.", file=sys.stderr)
        return 1
    finally:
        await embedder.aclose()
        await db.close()

    print(f"добавлено {len(report.added)}: {', '.join(report.added) or '-'}")
    print(f"обновлено {len(report.updated)}: {', '.join(report.updated) or '-'}")
    print(f"без изменений {len(report.skipped)}")
    print(f"удалено {len(report.deleted)}: {', '.join(report.deleted) or '-'}")
    print(f"кусков записано {report.chunks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
