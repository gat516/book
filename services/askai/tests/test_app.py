from __future__ import annotations

import httpx
import pytest

from novel_llm import Class, Completion

from askai.app import INSUFFICIENT, AskRequest, Service, create_app
from askai.config import Config
from askai.retrieval import Source, build_context


class FakeProvider:
    def __init__(self) -> None:
        self.classes: list[Class] = []
        self.completed = 0

    async def embed(self, texts, *, cls=Class.BATCH):
        self.classes.append(cls)
        return [[0.0, 0.0] for _ in texts]

    async def complete(self, prompt, *, system="", json_mode=False, cls=Class.BATCH, pin_model=False, model=None):
        self.classes.append(cls)
        self.completed += 1
        return Completion("answer", "fake", model or "fake-model")


def config() -> Config:
    return Config("postgres://unused", "secret", "ask-model", 2, "127.0.0.1", 8082)


def test_context_budget_keeps_source_attribution() -> None:
    context, sources = build_context([Source("chunk", 1, 2, "abc"), Source("fact", 2, 2, "long text")], 20)
    assert "[chunk:1 ch:2]" in context
    assert sources == [{"kind": "chunk", "id": 1, "chapter": 2}]


@pytest.mark.asyncio
async def test_internal_authentication_happens_before_service_call() -> None:
    provider = FakeProvider()
    service = Service(config(), provider)
    called = False

    async def ask(_: AskRequest):
        nonlocal called
        called = True
        raise AssertionError("must not call service")

    service.ask = ask  # type: ignore[method-assign]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test") as client:
        response = await client.post("/ask", json={"novel_id": "n", "question": "q", "at": 1})
    assert response.status_code == 401
    assert not called


@pytest.mark.asyncio
async def test_empty_retrieval_does_not_complete(monkeypatch) -> None:
    provider = FakeProvider()
    service = Service(config(), provider)

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr("askai.app.retrieve", empty)

    class Connection:
        async def execute(self, *args): pass
        def transaction(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class Pool:
        def connection(self): return Connection()

    service.pool = Pool()  # type: ignore[assignment]
    response = await service.ask(AskRequest(novel_id="n", question="q", at=1))
    assert response.answer == INSUFFICIENT
    assert provider.completed == 0
    assert provider.classes == [Class.INTERACTIVE]
