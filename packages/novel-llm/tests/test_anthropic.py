"""AnthropicProvider's api_key wiring (PLAN.md Phase N3): an explicit key must reach the
SDK client, and omitting it must still work (falls back to ANTHROPIC_API_KEY env, the
existing pre-Phase-N3 behavior) — no live call in either case.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import novel_llm.hosted as hosted
from novel_llm.anthropic import AnthropicProvider


async def test_explicit_api_key_is_used_by_litellm(monkeypatch):
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="claude-haiku-4-5", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="ok"))], usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="explicit-key")
    await provider.complete("source")
    assert calls[0]["api_key"] == "explicit-key"


async def test_no_api_key_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="claude-haiku-4-5", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="ok"))], usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = AnthropicProvider(model="claude-haiku-4-5")
    await provider.complete("source")
    assert calls[0]["api_key"] == "env-key"


async def test_schema_request_is_preserved_as_prompt_guidance(monkeypatch):
    provider = AnthropicProvider(model="model", api_key="test-key")
    schema = {"type": "object", "required": ["facts"]}
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="model", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"facts":[]}'))], usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    await provider.complete("source", system="stable", json_schema=schema)
    kwargs = calls[0]
    assert json.dumps(schema) in kwargs["messages"][0]["content"]
    assert kwargs["messages"][1] == {"role": "user", "content": "source"}
