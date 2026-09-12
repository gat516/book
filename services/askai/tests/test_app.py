from __future__ import annotations

import httpx
import pytest

from novel_llm import AdmissionRejected, Class, Completion, GeminiProvider

from askai.app import EmbeddingUnavailable, INSUFFICIENT, AskRequest, Service, create_app
import askai.app as app_module
from askai.config import Config
from askai.retrieval import Source, build_context


def _status_error(code: int, *, body: str = "", path: str = "/chat/completions",
                  headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    response = httpx.Response(
        code,
        headers=headers,
        content=body.encode(),
        request=httpx.Request("POST", "https://provider.example" + path),
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


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


def test_app_from_env_routes_gemini_completion_separately(monkeypatch) -> None:
    captured = {}
    cfg = Config(
        "postgres://unused", "secret", "ask-model", 2, "127.0.0.1", 8082,
        llm_provider="gemini", gemini_api_key="gemini-key", embed_provider="ollama",
    )
    monkeypatch.setattr(app_module, "load_config", lambda: cfg)
    monkeypatch.setattr(app_module, "create_app", lambda service: captured.setdefault("service", service))

    app_module.app_from_env()

    assert isinstance(captured["service"].provider, GeminiProvider)
    assert captured["service"].embed_provider is not captured["service"].provider


def test_context_budget_keeps_source_attribution() -> None:
    context, sources = build_context([Source("chunk", 1, 2, "abc"), Source("fact", 2, 2, "long text")], 20)
    assert "[chunk:1 ch:2]" in context
    assert sources == [{"kind": "chunk", "id": 1, "chapter": 2}]


def test_event_context_keeps_exact_evidence() -> None:
    evidence = {"quote": "Ares handed Abaddon the lotus.", "char_start": 10, "char_end": 41}
    context, sources = build_context([Source("event", "event-id", 7, "handed (giver: Ares, recipient: Abaddon)", evidence)], 500)
    assert "[event:event-id ch:7]" in context
    assert sources[0]["evidence"] == evidence


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

    class Cursor:
        async def fetchone(self):
            return None  # no novel_provider_config row -> Service falls back to self.provider

    class Connection:
        async def execute(self, *args):
            return Cursor()
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


@pytest.mark.asyncio
async def test_startup_does_not_wedge_on_embedding_outage(monkeypatch):
    from unittest.mock import AsyncMock
    from novel_llm import AdmissionRejected
    provider=FakeProvider()
    provider.embed=AsyncMock(side_effect=AdmissionRejected(retry_after_s=5))
    service=Service(config(),provider)
    service.pool.open=AsyncMock()
    sleep=AsyncMock()
    monkeypatch.setattr('askai.app.asyncio.sleep',sleep)
    await service.start()
    assert provider.embed.await_count==1
    sleep.assert_not_awaited()
    assert service.embedding_ready is False
    service.pool.open.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "category"),
    [
        (_status_error(401, body="secret-key=never-echo"), 502, "credential_rejected"),
        (_status_error(429, body='{"error":{"details":[{"retryDelay":"86400s"}]}}'),
         429, "quota_exhausted"),
        (AdmissionRejected("quota_exhausted", category="quota_exhausted"), 429, "quota_exhausted"),
        # AdmissionRejected now carries the bounded category; its legacy-looking
        # message must not override the explicit/default category.
        (AdmissionRejected("429 from provider: daily quota exceeded"), 429, "rate_limited"),
    ],
)
async def test_provider_failures_return_only_safe_categories(failure, status, category):
    service = Service(config(), FakeProvider())

    async def ask(_: AskRequest):
        raise failure

    service.ask = ask  # type: ignore[method-assign]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ask",
            json={"novel_id": "n", "question": "q", "at": 1},
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == status
    assert response.json() == {"error": "ask-ai provider failure", "category": category}
    assert "secret-key" not in response.text


@pytest.mark.asyncio
async def test_embedding_outage_returns_bounded_semantic_retrieval_error():
    service = Service(config(), FakeProvider())

    async def ask(_: AskRequest):
        raise EmbeddingUnavailable("unavailable")

    service.ask = ask  # type: ignore[method-assign]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ask", json={"novel_id": "n", "question": "q", "at": 1},
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 502
    assert response.json() == {"error": "semantic retrieval unavailable", "category": "unavailable"}


@pytest.mark.asyncio
async def test_missing_provider_credential_is_named():
    service = Service(config(), FakeProvider())

    async def ask(_: AskRequest):
        raise RuntimeError("GeminiProvider needs an api_key or GEMINI_API_KEY")

    service.ask = ask  # type: ignore[method-assign]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ask",
            json={"novel_id": "n", "question": "q", "at": 1},
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 502
    assert response.json() == {"error": "ask-ai provider failure", "category": "credential_missing"}
