"""Фейковый OpenAI-совместимый API как настоящий HTTP-сервер.

Эмбеддинги детерминированные: слова раскладываются по корзинам хешем, так что
у текстов с общими словами косинусная близость выше. Этого хватает, чтобы
проверить поиск, не ходя к настоящей модели.

Ответ чата задаётся тестом: готовый JSON, функция от запроса или сбой.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

EMPTY_PROFILE_UPDATES = {
    "name": None, "goal": None, "level": None, "timezone": None,
    "preferred_time": None, "format_interest": None, "is_teen": None,
}

DEFAULT_REPLY = {
    "sources": [],
    "needs_human": False,
    "handoff_reason": None,
    "is_smalltalk": True,
    "reply": "Фейковый ответ модели",
    "profile_updates": EMPTY_PROFILE_UPDATES,
    "lead": None,
}

ChatHandler = Callable[[dict[str, Any]], "dict[str, Any] | str"]


def fake_embedding(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha1(word.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [x / norm for x in vector]


def schema_name_of(body: dict[str, Any]) -> str:
    return ((body.get("response_format") or {}).get("json_schema") or {}).get("name", "")


class FakeLLM:
    """Три вида вызова: ответ клиенту, переписывание вопроса, резюме.

    Очереди ответов раздельные: тест задаёт ответ модели, не заботясь о том,
    сколько служебных вызовов сделает бот по дороге.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.chat_requests: list[dict[str, Any]] = []
        self.embed_requests: list[dict[str, Any]] = []
        # Очередь ответов на вопрос клиента. Пустая: DEFAULT_REPLY.
        self.replies: list[dict[str, Any] | str | ChatHandler] = []
        self.rewrites: list[dict[str, Any] | str | ChatHandler] = []
        self.summaries: list[dict[str, Any] | str | ChatHandler] = []
        # Код ошибки для следующих вызовов чата, None: работать нормально.
        self.chat_status: int | None = None
        # Вид вызова, который должен падать, None: падают все.
        self.failing_schema: str | None = None

    def requests_for(self, schema: str) -> list[dict[str, Any]]:
        return [b for b in self.chat_requests if schema_name_of(b) == schema]

    @property
    def answer_requests(self) -> list[dict[str, Any]]:
        return self.requests_for("manager_reply")

    def system_prompt(self, index: int = -1) -> str:
        return self.answer_requests[index]["messages"][0]["content"]

    def user_message(self, index: int = -1) -> str:
        return self.answer_requests[index]["messages"][-1]["content"]

    def history(self, index: int = -1) -> list[tuple[str, str]]:
        """Реплики между системным промптом и текущим сообщением клиента."""
        messages = self.answer_requests[index]["messages"]
        return [(m["role"], m["content"]) for m in messages[1:-1]]


def label_for(system_prompt: str, needle: str) -> str:
    """Метка блока [S1]/[L1], в котором встречается needle. Для сценариев тестов."""
    for match in re.finditer(r"\[(S\d+|L\d+)\]\n(.*?)(?=\n\[(?:S|L)\d+\]\n|\Z)", system_prompt, re.S):
        if needle in match.group(2):
            return match.group(1)
    raise AssertionError(f"в промпте нет блока с {needle!r}")


def build_app(state: FakeLLM) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/embeddings")
    async def embeddings(request: Request) -> JSONResponse:
        body = await request.json()
        state.embed_requests.append(body)
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {"object": "embedding", "index": i, "embedding": fake_embedding(t, state.dim)}
            for i, t in enumerate(texts)
        ]
        tokens = sum(len(t.split()) for t in texts)
        return JSONResponse({"data": data, "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})

    @app.post("/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        state.chat_requests.append(body)
        schema = schema_name_of(body)
        if state.chat_status is not None and state.failing_schema in (None, schema):
            return JSONResponse({"error": {"code": state.chat_status, "message": "fake"}}, status_code=state.chat_status)

        if schema == "search_query":
            planned = state.rewrites.pop(0) if state.rewrites else {"query": body["messages"][-1]["content"]}
        elif schema == "client_summary":
            planned = state.summaries.pop(0) if state.summaries else {"summary": "Фейковое резюме клиента"}
        else:
            planned = state.replies.pop(0) if state.replies else DEFAULT_REPLY
        if callable(planned):
            planned = planned(body)
        content = planned if isinstance(planned, str) else json.dumps(planned, ensure_ascii=False)
        return JSONResponse(
            {
                "id": "fake",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": len(json.dumps(body)) // 4, "completion_tokens": len(content) // 4},
            }
        )

    return app
