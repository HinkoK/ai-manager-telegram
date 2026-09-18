"""Eval: гоняет вопросы из eval/cases.json через настоящий конвейер ответа.

Ходит в настоящую модель и настоящую базу знаний, поэтому стоит денег (доли
цента за прогон) и в pytest не входит. Telegram и переписка не участвуют:
вызовы модели пишутся в llm_calls без привязки к разговору.

Проверки механические, без модели-судьи: нужные подстроки есть, запрещённых
нет, флаг передачи руководителю и файлы-источники совпадают с ожиданием.
Для подбора SEARCH_MIN_SCORE печатается близость лучшего нужного куска и
лучшего постороннего.

Запуск из папки bot:
    .venv/bin/python scripts/eval.py              # все вопросы
    .venv/bin/python scripts/eval.py trap-kaspi   # выбранные по id
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.answer import AnswerService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest import ROOT_ENV, check_knowledge_schema  # noqa: E402
from app.knowledge import ALWAYS_IN_CONTEXT  # noqa: E402
from app.llm import OpenAICompatClient  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.repo import Repo  # noqa: E402

CASES = pathlib.Path(__file__).resolve().parents[1] / "eval" / "cases.json"


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def memory_of(case: dict[str, Any]) -> Memory:
    """Профиль, резюме и история задаются прямо в кейсе: базы разговоров тут нет."""
    return Memory(
        profile=case.get("profile") or {},
        summary=case.get("summary"),
        history=[(turn["role"], turn["text"]) for turn in case.get("history") or []],
    )


def check_lead(case: dict[str, Any], lead: dict[str, Any] | None) -> list[str]:
    problems = []
    if "lead_kind" in case:
        actual = lead.get("kind") if lead else None
        if actual != case["lead_kind"]:
            problems.append(f"вид заявки {actual}, ждали {case['lead_kind']}")
    for field in case.get("lead_fields", []):
        if not (lead or {}).get(field):
            problems.append(f"в заявке нет поля «{field}»")
    return problems


def check(case: dict[str, Any], text: str, meta: dict[str, Any]) -> list[str]:
    problems = []
    reply = normalize(text)
    expected_human = case.get("needs_human")
    if expected_human is not None and meta.get("needs_human") != expected_human:
        problems.append(f"needs_human={meta.get('needs_human')} ({meta.get('handoff_reason')}), ждали {expected_human}")
    if not meta.get("needs_human"):
        for needle in case.get("contains_all", []):
            if normalize(needle) not in reply:
                problems.append(f"нет «{needle}»")
        if case.get("contains_any") and not any(normalize(n) in reply for n in case["contains_any"]):
            problems.append(f"нет ни одного из {case['contains_any']}")
        paths = {source["path"] for source in meta.get("sources", [])}
        if case.get("sources_any") and not paths & set(case["sources_any"]):
            problems.append(f"источники {sorted(paths) or '-'}, ждали один из {case['sources_any']}")
    for needle in case.get("not_contains", []):
        if normalize(needle) in reply:
            problems.append(f"есть запрещённое «{needle}»")
    # Набор слов, которых не должно быть в одном сообщении: так ловим анкету
    # вместо вопроса по одному пункту.
    for group in case.get("not_together", []):
        if all(normalize(n) in reply for n in group):
            problems.append(f"в одном сообщении сразу: {', '.join(group)}")
    return problems


async def retrieval_diagnostics(
    settings: Settings, repo: Repo, embedder: OpenAICompatClient, case: dict[str, Any]
) -> str:
    expected = set(case.get("sources_any", []))
    if not expected:
        return ""
    vector = (await embedder.embed(
        model=settings.embedding_model, texts=[case["question"]], dim=settings.embedding_dim,
        provider=settings.embedding_provider,
    )).vectors[0]
    candidates = await repo.search_chunks(vector, 20, ALWAYS_IN_CONTEXT)
    rank = next((i for i, c in enumerate(candidates, 1) if c.path in expected), None)
    best_hit = next((c.score for c in candidates if c.path in expected), None)
    best_miss = next((c.score for c in candidates if c.path not in expected), None)
    hit = f"{best_hit:.3f} (место {rank})" if best_hit is not None else "нет в топ-20"
    miss = f"{best_miss:.3f}" if best_miss is not None else "-"
    return f"поиск: нужный кусок {hit}, лучший посторонний {miss}"


async def main() -> int:
    wanted = set(sys.argv[1:])
    cases = [c for c in json.loads(CASES.read_text(encoding="utf-8")) if not wanted or c["id"] in wanted]
    settings = Settings(_env_file=str(ROOT_ENV) if ROOT_ENV.exists() else None)
    setup_logging("ERROR", secrets=[settings.telegram_bot_token, settings.llm_api_key, settings.embedding_key])

    db = Database(settings.postgres_url, settings.school_schema, min_size=1, max_size=2)
    llm = OpenAICompatClient(settings.llm_base_url, settings.llm_api_key, settings.llm_timeout_sec)
    embedder = OpenAICompatClient(settings.embedding_url, settings.embedding_key, settings.llm_timeout_sec)
    await db.connect()
    repo = Repo(db)
    failed: list[str] = []
    tokens_in = tokens_out = 0
    try:
        if not await check_knowledge_schema(repo, settings):
            print("база знаний пуста: сначала python -m app.ingest", file=sys.stderr)
            return 2
        service = AnswerService(settings, repo, llm, embedder)
        print(f"модель {settings.llm_model}, размышление {settings.llm_reasoning_effort}, "
              f"эмбеддинги {settings.embedding_model}, top_k {settings.search_top_k}, "
              f"порог {settings.search_min_score}\n")
        for case in cases:
            answer = await service.answer(
                case["question"], conversation_id=None, message_id=None, memory=memory_of(case)
            )
            problems = check(case, answer.text, answer.meta) + check_lead(case, answer.lead)
            tokens_in += answer.meta.get("prompt_tokens") or 0
            tokens_out += answer.meta.get("completion_tokens") or 0
            mark = "OK  " if not problems else "FAIL"
            if problems:
                failed.append(case["id"])
            sources = ", ".join(sorted({s["path"] for s in answer.meta.get("sources", [])})) or "-"
            lead = ", ".join(f"{k}={v}" for k, v in (answer.lead or {}).items() if v) or "нет"
            print(f"{mark} {case['id']}  [{answer.meta.get('latency_ms', '?')} мс, "
                  f"needs_human={answer.needs_human}, источники: {sources}, заявка: {lead}]")
            print(f"     вопрос: {case['question']}")
            if answer.meta.get("search_query"):
                print(f"     в поиск: {answer.meta['search_query']}")
            print(f"     ответ:  {answer.text}")
            diagnostics = await retrieval_diagnostics(settings, repo, embedder, case)
            if diagnostics:
                print(f"     {diagnostics}")
            for problem in problems:
                print(f"     - {problem}")
            print()
    finally:
        await llm.aclose()
        await embedder.aclose()
        await db.close()

    print(f"итого: {len(cases) - len(failed)} из {len(cases)}, токены модели {tokens_in} вход / {tokens_out} выход")
    if failed:
        print("не прошли:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
