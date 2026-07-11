"""The load-bearing test of this phase (PLAN.md §1.2, §12 risk #4): the idempotency key
must change when the model/provider changes, or a backend switch would serve stale output
under a still-valid key. Pure — no DB, no network.
"""

from __future__ import annotations

import dataclasses

from pipeline.config import Config
from pipeline.jobs import idempotency_key, model_id_for_stage

RAW_HASH = "sha256:abc123"


def _cfg(**overrides) -> Config:
    base = dict(
        database_url="postgres://x",
        redis_url="redis://x",
        object_endpoint="localhost:9000",
        object_access_key="k",
        object_secret_key="s",
        object_bucket="raw-chapters",
        object_secure=False,
        llm_provider="ollama",
        llm_model_translate="qwen2.5:14b",
        llm_model_extract="qwen2.5:14b",
        embed_model="nomic-embed-text",
        ollama_host="http://localhost:11434",
        prompt_version="1",
        config_version="1",
        queue_timeout=5,
    )
    base.update(overrides)
    return Config(**base)


def test_same_config_same_key():
    a = idempotency_key("state", RAW_HASH, _cfg())
    b = idempotency_key("state", RAW_HASH, _cfg())
    assert a == b


def test_provider_switch_changes_key():
    ollama = idempotency_key("state", RAW_HASH, _cfg(llm_provider="ollama"))
    anthropic = idempotency_key("state", RAW_HASH, _cfg(llm_provider="anthropic"))
    assert ollama != anthropic


def test_model_switch_changes_key():
    a = idempotency_key("state", RAW_HASH, _cfg(llm_model_extract="qwen2.5:14b"))
    b = idempotency_key("state", RAW_HASH, _cfg(llm_model_extract="claude-haiku-4-5"))
    assert a != b


def test_prompt_version_bump_changes_key():
    a = idempotency_key("state", RAW_HASH, _cfg(prompt_version="1"))
    b = idempotency_key("state", RAW_HASH, _cfg(prompt_version="2"))
    assert a != b


def test_translate_key_differs_from_extract_key():
    # translate uses LLM_MODEL_TRANSLATE, extract uses LLM_MODEL_EXTRACT — different
    # models => different stage-dependent model_id => different key.
    cfg = _cfg(llm_model_translate="claude-opus-4-8", llm_model_extract="claude-haiku-4-5")
    assert idempotency_key("translate", RAW_HASH, cfg) != idempotency_key("state", RAW_HASH, cfg)
    assert model_id_for_stage("translate", cfg) == "ollama:claude-opus-4-8"
    assert model_id_for_stage("state", cfg) == "ollama:claude-haiku-4-5"


def test_different_stage_changes_key():
    cfg = _cfg()
    assert idempotency_key("resolve", RAW_HASH, cfg) != idempotency_key("state", RAW_HASH, cfg)
