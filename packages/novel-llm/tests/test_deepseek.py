"""DeepSeekProvider against a mocked httpx transport — no real network call.

PLAN.md Phase N3's own "done when" criterion for this provider.
"""

from __future__ import annotations

import json
import pytest
from types import SimpleNamespace

import novel_llm.hosted as hosted
from novel_llm.deepseek import DeepSeekProvider


async def test_complete_returns_served_identity_and_usage(monkeypatch):
    provider = DeepSeekProvider(model="deepseek-chat", api_key="test-key")
    async def complete(**kwargs):
        assert kwargs["api_key"] == "test-key"
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content="hello"))], model="deepseek-chat",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                   prompt_cache_hit_tokens=3),
            _hidden_params={"custom_llm_provider": "deepseek"})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))

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


async def test_schema_request_uses_explicit_prompt_fallback_and_json_mode(monkeypatch):
    provider = DeepSeekProvider(model="model", api_key="test-key")
    schema = {"type": "object", "required": ["facts"]}

    async def complete(**payload):
        assert payload["response_format"] == {"type": "json_object"}
        assert json.dumps(schema, ensure_ascii=False) in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"] == "source"
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content='{"facts":[]}'))], model="model", usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    assert (await provider.complete("source", system="stable", json_schema=schema)).text == '{"facts":[]}'
