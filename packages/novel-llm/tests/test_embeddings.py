from __future__ import annotations

import pytest

import novel_llm.hosted as hosted
from novel_llm.gemini import GeminiProvider
from novel_llm.openrouter import OpenRouterProvider
from novel_llm.provider import Class


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return self.response


@pytest.mark.asyncio
async def test_openrouter_uses_openai_embeddings_path_and_preserves_indices(monkeypatch):
    client = _Client(_Response({"data": [
        {"index": 1, "embedding": [2.0, 3.0]},
        {"index": 0, "embedding": [0.0, 1.0]},
    ]}))
    monkeypatch.setattr(hosted.httpx, "AsyncClient", lambda **kwargs: client)
    provider = OpenRouterProvider(model="openai/text-embedding-3-small", api_key="secret", embed_dim=2)

    vectors = await provider.embed(["one", "two"])

    assert vectors == [[0.0, 1.0], [2.0, 3.0]]
    url, headers, payload = client.calls[0]
    assert url == "https://openrouter.ai/api/v1/embeddings"
    assert headers["Authorization"] == "Bearer secret"
    assert payload == {"model": "openai/text-embedding-3-small", "input": ["one", "two"], "dimensions": 2}


@pytest.mark.asyncio
async def test_gemini_uses_native_batch_embeddings_path_and_output_dimension(monkeypatch):
    client = _Client(_Response({"embeddings": [{"values": [0.0, 1.0]}, {"values": [2.0, 3.0]}]}))
    monkeypatch.setattr(hosted.httpx, "AsyncClient", lambda **kwargs: client)
    provider = GeminiProvider(
        model="gemini-2.5-flash", embed_model="gemini-embedding-001", embed_dim=2,
        api_key="secret",
    )

    vectors = await provider.embed(["one", "two"])

    assert vectors == [[0.0, 1.0], [2.0, 3.0]]
    url, headers, payload = client.calls[0]
    assert url.endswith("/v1beta/models/gemini-embedding-001:batchEmbedContents")
    assert headers["x-goog-api-key"] == "secret"
    assert payload["requests"][0] == {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": "one"}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": 2,
    }

    await provider.embed(["question", "question-2"], cls=Class.INTERACTIVE)
    assert client.calls[1][2]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_hosted_embedding_providers_require_credentials(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        OpenRouterProvider(model="embedding-model")
    with pytest.raises(RuntimeError):
        GeminiProvider(model="embedding-model")
