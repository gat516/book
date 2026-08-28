"""DeepSeekProvider against a mocked httpx transport — no real network call.

PLAN.md Phase N3's own "done when" criterion for this provider.
"""

from __future__ import annotations

import httpx
import json
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


async def test_schema_request_uses_explicit_prompt_fallback_and_json_mode():
    provider = DeepSeekProvider(model="model", api_key="test-key")
    await provider._client.aclose()
    schema = {"type": "object", "required": ["facts"]}

    def handle(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert json.dumps(schema) in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"] == "source"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"facts":[]}'}}]})

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        assert (await provider.complete("source", system="stable", json_schema=schema)).text == '{"facts":[]}'
    finally:
        await provider.aclose()
