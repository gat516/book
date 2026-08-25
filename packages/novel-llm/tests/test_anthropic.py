"""AnthropicProvider's api_key wiring (PLAN.md Phase N3): an explicit key must reach the
SDK client, and omitting it must still work (falls back to ANTHROPIC_API_KEY env, the
existing pre-Phase-N3 behavior) — no live call in either case.
"""

from __future__ import annotations

from novel_llm.anthropic import AnthropicProvider


def test_explicit_api_key_is_used_by_the_sdk_client():
    provider = AnthropicProvider(model="claude-haiku-4-5", api_key="explicit-key")
    assert provider._client.api_key == "explicit-key"


def test_no_api_key_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    provider = AnthropicProvider(model="claude-haiku-4-5")
    assert provider._client.api_key == "env-key"
