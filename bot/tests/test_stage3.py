"""Этап 3: знания и поиск.

Модель и эмбеддинги фейковые (tests/fake_llm.py), база и код настоящие. Здесь
проверяем то, что не зависит от ума модели: нарезку, загрузку, поиск, что
попадает в промпт, и что код не верит модели на слово. Качество ответов
настоящей модели проверяет scripts/eval.py.
"""

from __future__ import annotations

import json

import pytest

from app import texts
from app.db import Database
from app.ingest import ingest
from app.knowledge import default_knowledge_dir, load_documents, parse_document
from app.llm import OpenAICompatClient
from app.repo import Repo
from scripts.migrate import apply_migrations
from tests.conftest import (
    CLIENT_ID, TEST_EMBEDDING_DIM, TEST_LLM_KEY, TEST_PROVIDER, TEST_SCHEMA, wait_until,
)
from tests.fake_llm import label_for
from tests.fake_telegram import message_update

S = TEST_SCHEMA
MODEL = "test/embedding-model"


async def batch_done(state) -> bool:
    return await wait_until(lambda: state.pending == [])


@pytest.fixture
async def kb(database, db_admin, llm):
    llm_state, llm_server = llm
    db = Database(database.bot_url, database.schema, min_size=1, max_size=2)
    await db.connect()
    embedder = OpenAICompatClient(llm_server.base_url, TEST_LLM_KEY, 5.0)
    try:
        yield Repo(db), embedder, llm_state
    finally:
        await embedder.aclose()
        await db.close()


async def load_real_knowledge(kb, model: str = MODEL):
    repo, embedder, _llm_state = kb
    documents = load_documents(default_knowledge_dir())
    await ingest(repo, embedder, documents, model=model, dim=TEST_EMBEDDING_DIM)
    return documents


# --- нарезка ----------------------------------------------------------------


def test_chunker_keeps_sections_tables_and_prefix():
    text = (
        "# Стоимость\n\nВсе цены в долларах.\n\n"
        "## С носителем\n\n| Пакет | Цена |\n|---|---|\n| 16 занятий | $400 |\n\n"
        "### Примечание\n\nТолько Zoom.\n\n"
        "## Пустой\n\n"
        "## Пары\n\nПлюс 50%.\n"
    )
    doc = parse_document("pricing.md", text)

    assert doc.title == "Стоимость"
    assert [c.section_title for c in doc.chunks] == ["Стоимость", "С носителем", "Пары"]
    assert [c.index for c in doc.chunks] == [0, 1, 2]
    native = doc.chunks[1].content
    assert native.startswith("Файл: pricing.md (Стоимость)\nРаздел: С носителем\n\n")
    # Таблица и подраздел ### остаются в куске своего раздела.
    assert "| 16 занятий | $400 |" in native
    assert "### Примечание" in native
    assert parse_document("pricing.md", text + "\nещё строка").content_hash != doc.content_hash


def test_real_knowledge_base_is_cut_by_sections():
    documents = load_documents(default_knowledge_dir())
    paths = {d.path for d in documents}
    assert "README.md" not in paths
    assert {"pricing.md", "limitations.md", "faq.md"} <= paths

    chunks = [c for d in documents for c in d.chunks]
    assert all(c.content.startswith("Файл: ") for c in chunks)
    # Главная ловушка с ценами: $400 лежит в куске раздела про носителя.
    native = [c for c in chunks if c.section_title == "Индивидуальные занятия с носителем языка"]
    assert len(native) == 1
    assert "| 16 занятий | $400 | $25 |" in native[0].content


async def test_migrations_refuse_dimension_over_hnsw_limit():
    with pytest.raises(ValueError):
        await apply_migrations(None, "school_x", "role_x", 3072)  # type: ignore[arg-type]


# --- загрузка и поиск -------------------------------------------------------


async def test_ingest_skips_unchanged_and_follows_changes(tmp_path, kb, db_admin):
    repo, embedder, llm_state = kb
    (tmp_path / "pricing.md").write_text(
        "# Цены\n\n## Группы\n\n| Пакет | Цена |\n|---|---|\n| 8 | $72 |\n\n## Пары\n\nПлюс 50%.\n",
        encoding="utf-8",
    )
    (tmp_path / "schedule.md").write_text("# Расписание\n\nВступление.\n\n## Утро\n\nС 7:00.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Шпаргалка к тестам\n\n## Ловушки\n\nTOEFL: нет.\n", encoding="utf-8")

    first = await ingest(repo, embedder, load_documents(tmp_path), model=MODEL, dim=TEST_EMBEDDING_DIM)
    assert sorted(first.added) == ["pricing.md", "schedule.md"]
    assert first.chunks == 4
    embed_calls = len(llm_state.embed_requests)

    again = await ingest(repo, embedder, load_documents(tmp_path), model=MODEL, dim=TEST_EMBEDDING_DIM)
    assert sorted(again.skipped) == ["pricing.md", "schedule.md"]
    assert len(llm_state.embed_requests) == embed_calls

    (tmp_path / "schedule.md").write_text("# Расписание\n\nВступление.\n\n## Утро\n\nС 8:00.\n", encoding="utf-8")
    changed = await ingest(repo, embedder, load_documents(tmp_path), model=MODEL, dim=TEST_EMBEDDING_DIM)
    assert changed.updated == ["schedule.md"] and changed.skipped == ["pricing.md"]
    assert len(llm_state.embed_requests) == embed_calls + 1
    assert await db_admin.fetchval(
        f"select count(*) from {S}.knowledge_chunks where content like '%С 8:00%'"
    ) == 1

    (tmp_path / "pricing.md").unlink()
    removed = await ingest(repo, embedder, load_documents(tmp_path), model=MODEL, dim=TEST_EMBEDDING_DIM)
    assert removed.deleted == ["pricing.md"]
    assert await db_admin.fetchval(f"select count(*) from {S}.knowledge_chunks") == 2

    other_model = await ingest(repo, embedder, load_documents(tmp_path), model="other/model", dim=TEST_EMBEDDING_DIM)
    assert other_model.updated == ["schedule.md"]

    with pytest.raises(ValueError):
        await ingest(repo, embedder, [], model=MODEL, dim=TEST_EMBEDDING_DIM)
    assert await db_admin.fetchval(f"select count(*) from {S}.knowledge_documents") == 1
    assert await db_admin.fetchval(f"select count(*) from {S}.llm_calls where purpose = 'embed'") == 4


async def test_search_ranks_matching_section_and_skips_limitations(tmp_path, kb):
    repo, embedder, _llm_state = kb
    (tmp_path / "limitations.md").write_text("# Чего не делаем\n\n## Экзамены\n\nНе готовим к TOEFL.\n", encoding="utf-8")
    (tmp_path / "pricing.md").write_text(
        "# Цены\n\n## Носитель\n\nсколько стоит 16 занятий с носителем: $400\n\n"
        "## Группа\n\nгрупповые занятия по вечерам\n",
        encoding="utf-8",
    )
    (tmp_path / "schedule.md").write_text("# Расписание\n\n## Утро\n\nутренние группы в 8:00\n", encoding="utf-8")
    await ingest(repo, embedder, load_documents(tmp_path), model=MODEL, dim=TEST_EMBEDDING_DIM)

    query = await embedder.embed(model=MODEL, texts=["сколько стоит 16 занятий с носителем"], dim=TEST_EMBEDDING_DIM)
    found = await repo.search_chunks(query.vectors[0], 8, "limitations.md")

    assert (found[0].path, found[0].section_title) == ("pricing.md", "Носитель")
    assert [c.score for c in found] == sorted((c.score for c in found), reverse=True)
    assert all(c.path != "limitations.md" for c in found)
    limitations = await repo.document_chunks("limitations.md")
    assert [c.section_title for c in limitations] == ["Экзамены"]


# --- конвейер ответа через бота ----------------------------------------------


async def test_question_is_answered_from_found_sections(fake_tg, llm, start_bot, kb, db_admin):
    state, _tg_server = fake_tg
    llm_state, _llm_server = llm
    documents = await load_real_knowledge(kb)

    def reply(body):
        label = label_for(body["messages"][0]["content"], "Индивидуальные занятия с носителем языка")
        return {
            "sources": [label], "needs_human": False, "handoff_reason": None, "is_smalltalk": False,
            "reply": "16 занятий с носителем стоят $400, это $25 за занятие.",
        }

    llm_state.replies.append(reply)
    await start_bot()
    question = "Сколько стоит пакет 16 занятий с носителем языка?"
    state.push(message_update(1, user_id=CLIENT_ID, text=question))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    assert state.texts_to(CLIENT_ID)[0] == "16 занятий с носителем стоят $400, это $25 за занятие."

    body = llm_state.chat_requests[-1]
    assert body["model"] == "test/chat-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning"] == {"effort": "none"}
    assert body["provider"] == TEST_PROVIDER
    assert llm_state.user_message() == question

    prompt = llm_state.system_prompt()
    assert "Готовим только к IELTS" in prompt  # limitations.md всегда в контексте
    assert "Что в базе намеренно нет" not in prompt  # README в базу не попал
    assert prompt.count("\n[S") <= 8
    whole_base = sum(len(c.content) for d in documents for c in d.chunks)
    assert len(prompt) < whole_base / 2  # всю базу в промпт не кладём

    meta = json.loads(await db_admin.fetchval(f"select meta from {S}.messages where role = 'bot'"))
    assert meta["sources"][0]["path"] == "pricing.md"
    assert meta["needs_human"] is False and meta["handoff_reason"] is None

    client_message_id = await db_admin.fetchval(f"select id from {S}.messages where role = 'client'")
    calls = await db_admin.fetch(
        f"select purpose, message_id, completion_tokens, error from {S}.llm_calls "
        "where message_id is not null order by id"
    )
    assert [c["purpose"] for c in calls] == ["embed", "answer"]
    assert {c["message_id"] for c in calls} == {client_message_id}
    assert calls[1]["completion_tokens"] > 0 and calls[1]["error"] is None


async def test_model_asks_for_human(bot, llm, kb, db_admin):
    state, _server, settings = bot
    llm_state, _ = llm
    await load_real_knowledge(kb)
    llm_state.replies.append({
        "sources": [], "needs_human": True, "handoff_reason": "not_in_knowledge",
        "is_smalltalk": False, "reply": "",
    })
    state.push(message_update(1, user_id=CLIENT_ID, text="Можно оплатить картой Kaspi из Казахстана?"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    event = await db_admin.fetchrow(f"select payload from {S}.events where type = 'handoff_started'")
    assert json.loads(event["payload"]) == {"reason": "not_in_knowledge"}
    meta = json.loads(await db_admin.fetchval(f"select meta from {S}.messages where role = 'bot'"))
    assert meta["needs_human"] is True


@pytest.mark.parametrize(
    ("sources", "expected_unknown"),
    [([], 0), (["S99"], 1)],
    ids=["без источников", "выдуманная метка"],
)
async def test_answer_without_real_sources_becomes_handoff(bot, llm, kb, db_admin, sources, expected_unknown):
    """Модель уверенно выдумала скидку и не опёрлась ни на один показанный кусок."""
    state, _server, settings = bot
    llm_state, _ = llm
    await load_real_knowledge(kb)
    llm_state.replies.append({
        "sources": sources, "needs_human": False, "handoff_reason": None,
        "is_smalltalk": False, "reply": "Для студентов скидка 20%!",
    })
    state.push(message_update(1, user_id=CLIENT_ID, text="Есть скидка для студентов?"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    assert "20%" not in state.texts_to(CLIENT_ID)[0]
    meta = json.loads(await db_admin.fetchval(f"select meta from {S}.messages where role = 'bot'"))
    assert meta["handoff_reason"] == "no_sources"
    assert meta["unknown_sources"] == expected_unknown


async def test_model_failure_does_not_kill_bot(bot, llm, db_admin):
    state, _server, settings = bot
    llm_state, _ = llm
    llm_state.chat_status = 500
    state.push(message_update(1, user_id=CLIENT_ID, text="Сколько стоит?"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    assert await batch_done(state)
    assert await db_admin.fetchval(f"select error from {S}.llm_calls where purpose = 'answer'") == "http_500"

    # Сбой модели передал разговор владельцу (этап 6), поэтому возвращаем его боту.
    assert await db_admin.fetchval(f"select state from {S}.conversations") == "HUMAN_REQUESTED"
    await db_admin.execute(f"update {S}.conversations set state = 'AI_ACTIVE'")

    llm_state.chat_status = None
    state.push(message_update(2, user_id=CLIENT_ID, text="А сейчас?"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 2)
    assert state.texts_to(CLIENT_ID)[1] == "Фейковый ответ модели"


async def test_broken_json_from_model_gives_neutral_text(bot, llm, db_admin):
    state, _server, settings = bot
    llm_state, _ = llm
    llm_state.replies.append("это не json")
    state.push(message_update(1, user_id=CLIENT_ID, text="Сколько стоит?"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    assert state.texts_to(CLIENT_ID)[0] == texts.client_handoff(settings)
    assert await db_admin.fetchval(f"select error from {S}.llm_calls where purpose = 'answer'") == "invalid_json"


async def test_greeting_and_voice_do_not_call_model(bot, llm):
    state, _server, _settings = bot
    llm_state, _ = llm
    state.push(message_update(1, user_id=CLIENT_ID, text="/start"))
    state.push(message_update(2, user_id=CLIENT_ID, text=None, extra={"voice": {"file_id": "a", "duration": 1, "file_unique_id": "u"}}))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 2)
    assert await batch_done(state)
    assert llm_state.chat_requests == [] and llm_state.embed_requests == []


async def test_logs_have_no_client_text_and_no_keys(fake_tg, start_bot, capsys):
    state, _ = fake_tg
    await start_bot(log_level="INFO")
    state.push(message_update(1, user_id=CLIENT_ID, text="Секретный вопрос про оплату 7391"))
    assert await wait_until(lambda: len(state.texts_to(CLIENT_ID)) == 1)
    assert await batch_done(state)

    output = capsys.readouterr().out
    assert "ответ модели" in output  # логи INFO действительно пишутся
    assert '"found"' in output  # что нашёл поиск, видно по файлам и разделам
    assert "7391" not in output
    assert TEST_LLM_KEY not in output


async def test_bot_refuses_to_start_with_wrong_dimension(start_bot):
    with pytest.raises(RuntimeError):
        await start_bot(embedding_dim=TEST_EMBEDDING_DIM // 2)


async def test_bot_refuses_to_start_with_knowledge_from_other_model(start_bot, kb):
    await load_real_knowledge(kb, model="other/embedding-model")
    with pytest.raises(RuntimeError):
        await start_bot()
