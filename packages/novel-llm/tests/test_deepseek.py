"""DeepSeekProvider against a mocked httpx transport — no real network call.

PLAN.md Phase N3's own "done when" criterion for this provider.
"""

from __future__ import annotations

import httpx
import pytest

from novel_llm.deepseek import DeepSeekProvider


def _mock_transport(response_json: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.url.path == "/chat/completions"
        return httpx.Response(200, json=response_json)

    return httpx.MockTransport(handler)


async def test_complete_returns_served_identity_and_usage():
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")
    provider._client._transport = _mock_transport({
        "choices": [{"message": {"content": "hello"}}],
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 3},
    })

    completion = await provider.complete("hi")

    assert completion.text == "hello"
    assert completion.served_provider == "deepseek"
    assert completion.served_model == "deepseek-chat"
    assert completion.input_tokens == 10
    assert completion.output_tokens == 5
    assert completion.cache_read_tokens == 3


async def test_embed_raises_not_implemented():
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")
    with pytest.raises(NotImplementedError):
        await provider.embed(["text"])


def test_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        DeepSeekProvider(model="deepseek-chat", api_key=None)
