"""The load-bearing test of this phase (PLAN.md §1.2, §12 risk #4): the idempotency key
must change when the model/provider changes, or a backend switch would serve stale output
under a still-valid key. Pure — no DB, no network.
"""

from __future__ import annotations

import hashlib

import pytest
from fixtures import make_config

from pipeline.config import Config
from pipeline.jobs import (
    idempotency_key,
    model_for_stage,
    model_id_for_stage,
    stage_config_version,
)

RAW_HASH = "sha256:abc123"

def _key(stage: str, cfg: Config) -> str:
    return idempotency_key(stage, RAW_HASH, cfg)


_cfg = make_config


def test_same_config_same_key():
    a = _key("facts", _cfg())
    b = _key("facts", _cfg())
    assert a == b


def test_provider_switch_changes_key():
    ollama = _key("facts", _cfg(llm_provider="ollama"))
    anthropic = _key("facts", _cfg(llm_provider="anthropic"))
    assert ollama != anthropic


def test_model_switch_changes_key():
    a = _key("facts", _cfg(llm_model_extract="qwen2.5:14b"))
    b = _key("facts", _cfg(llm_model_extract="claude-haiku-4-5"))
    assert a != b


def test_served_model_override_changes_translation_key():
    cfg = _cfg(llm_model_translate="floating-alias")
    requested = idempotency_key("translate", RAW_HASH, cfg, glossary_version=3)
    served = idempotency_key(
        "translate",
        RAW_HASH,
        cfg,
        glossary_version=3,
        model_id="anthropic:concrete-snapshot",
    )
    assert requested != served


def test_prompt_version_bump_changes_key():
    a = _key("facts", _cfg(prompt_version="1"))
    b = _key("facts", _cfg(prompt_version="2"))
    assert a != b


def test_translate_key_differs_from_extract_key():
    # translate uses LLM_MODEL_TRANSLATE, extract uses LLM_MODEL_EXTRACT — different
    # models => different stage-dependent model_id => different key.
    cfg = _cfg(llm_model_translate="claude-opus-4-8", llm_model_extract="claude-haiku-4-5")
    assert _key("translate", cfg) != _key("facts", cfg)
    assert model_id_for_stage("translate", cfg) == "ollama:claude-opus-4-8"
    assert model_id_for_stage("state", cfg) == "ollama:claude-haiku-4-5"


def test_per_book_stage_models_route_and_key_independently():
    cfg = _cfg()
    models = {"translate": "qwen2.5:7b-instruct", "extract": "qwen3:4b-instruct-2507-q8_0"}
    assert model_for_stage("translate", cfg, models) == models["translate"]
    assert model_for_stage("state", cfg, models) == models["extract"]
    assert model_id_for_stage("translate", cfg, "ollama", models) != model_id_for_stage("state", cfg, "ollama", models)


def test_different_stage_changes_key():
    cfg = _cfg()
    assert _key("resolve", cfg) != _key("facts", cfg)


def test_key_uses_unit_separator_not_pipe():
    # §3.5 mandates 0x1F, not a printable delimiter like '|' — a printable separator can
    # legally appear inside a model id and make the join ambiguous in principle. Assert
    # against a hand-computed digest so a future refactor can't silently regress the format.
    cfg = _cfg()
    expected_parts = [
        "facts",
        RAW_HASH,
        cfg.prompt_version,
        stage_config_version("facts", cfg),
        model_id_for_stage("facts", cfg),
    ]
    expected = hashlib.sha256("\x1f".join(expected_parts).encode("utf-8")).hexdigest()
    assert _key("facts", cfg) == expected

    # A pipe-joined digest of the same parts must NOT match — proves the separator
    # actually changed, not just that some hash is being computed.
    pipe_digest = hashlib.sha256("|".join(expected_parts).encode("utf-8")).hexdigest()
    assert _key("facts", cfg) != pipe_digest


def test_model_for_stage_matches_model_id_for_stage():
    # jobs.model_for_stage is what stage code passes to complete(model=...); it must be
    # the same value model_id_for_stage embeds in the cache key, or a stage could ask the
    # provider for one model while the key claims another (the bug this fixes, §12).
    cfg = _cfg(llm_model_translate="claude-opus-4-8", llm_model_extract="claude-haiku-4-5")
    assert model_id_for_stage("translate", cfg) == f"{cfg.llm_provider}:{model_for_stage('translate', cfg)}"
    assert model_for_stage("translate", cfg) == "claude-opus-4-8"
    assert model_for_stage("state", cfg) == "claude-haiku-4-5"
