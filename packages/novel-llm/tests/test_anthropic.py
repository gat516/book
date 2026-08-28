"""AnthropicProvider's api_key wiring (PLAN.md Phase N3): an explicit key must reach the
SDK client, and omitting it must still work (falls back to ANTHROPIC_API_KEY env, the
existing pre-Phase-N3 behavior) — no live call in either case.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from novel_llm.anthropic import AnthropicProvider


def test_explicit_api_key_is_used_by_the_sdk_client():
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="explicit-key")
    assert provider._client.api_key == "explicit-key"


def test_no_api_key_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    provider = AnthropicProvider(model="claude-haiku-4-5")
    assert provider._client.api_key == "env-key"


async def test_schema_request_is_preserved_as_prompt_guidance(monkeypatch):
    provider = AnthropicProvider(model="model", api_key="test-key")
    schema = {"type": "object", "required": ["facts"]}
    create = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"facts":[]}')],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    ))
    monkeypatch.setattr(provider._client.messages, "create", create)
    try:
        await provider.complete("source", system="stable", json_schema=schema)
        kwargs = create.call_args.kwargs
        assert json.dumps(schema) in kwargs["system"]
        assert kwargs["messages"] == [{"role": "user", "content": "source"}]
    finally:
        await provider.aclose()
