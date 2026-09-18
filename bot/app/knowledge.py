"""Нарезка базы знаний на куски. Чистые функции, без базы и сети.

Кусок это раздел ## целиком, вместе с подразделами ### и таблицами. К каждому
куску приклеивается «файл / раздел»: без этого поиск путает цены разных
форматов, в прайсе «16 занятий» стоят и $256, и $400. Текст до первого ##
становится отдельным куском с названием файла.

README.md в базу не идёт: в нём прямо написано, какие вопросы ловушки и какие
ответы правильные. Модель начала бы цитировать шпаргалку к тестам.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

# Меняется, когда меняется нарезка: тогда все файлы перезагрузятся, даже
# если их текст не трогали.
CHUNKER_VERSION = "1"

EXCLUDED_FILES = frozenset({"README.md"})

# Этот файл модель получает всегда, а не только когда его нашёл поиск.
ALWAYS_IN_CONTEXT = "limitations.md"


@dataclass(frozen=True)
class Chunk:
    index: int
    section_title: str
    content: str


@dataclass(frozen=True)
class Document:
    path: str
    title: str
    content_hash: str
    chunks: tuple[Chunk, ...]


def content_hash(text: str) -> str:
    return hashlib.sha256(f"chunker:{CHUNKER_VERSION}\n{text}".encode("utf-8")).hexdigest()


def _chunk_text(path: str, title: str, section: str, body: str) -> str:
    return f"Файл: {path} ({title})\nРаздел: {section}\n\n{body}"


def parse_document(path: str, text: str) -> Document:
    lines = text.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), path)

    sections: list[tuple[str, list[str]]] = []
    intro: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            current = []
            sections.append((line[3:].strip(), current))
        elif current is not None:
            current.append(line)
        elif not line.startswith("# "):
            intro.append(line)

    chunks: list[Chunk] = []
    intro_body = "\n".join(intro).strip()
    if intro_body:
        chunks.append(Chunk(0, title, _chunk_text(path, title, title, intro_body)))
    for section_title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        chunks.append(
            Chunk(len(chunks), section_title, _chunk_text(path, title, section_title, body))
        )
    return Document(path=path, title=title, content_hash=content_hash(text), chunks=tuple(chunks))


def default_knowledge_dir() -> pathlib.Path:
    # bot/app/knowledge.py -> корень репозитория -> knowledge/
    return pathlib.Path(__file__).resolve().parents[2] / "knowledge"


def load_documents(directory: pathlib.Path) -> list[Document]:
    if not directory.is_dir():
        raise FileNotFoundError(f"нет папки базы знаний: {directory}")
    documents = []
    for file in sorted(directory.glob("*.md")):
        if file.name in EXCLUDED_FILES:
            continue
        documents.append(parse_document(file.name, file.read_text(encoding="utf-8")))
    return documents
