"""provider_from_env's backend selection (PLAN.md Phase N3: added the "deepseek" case)."""

from __future__ import annotations

from fixtures import make_config
from novel_llm import DeepSeekProvider, OllamaProvider
from pipeline.llm import provider_from_env


def test_deepseek_case_constructs_a_deepseek_provider():
    cfg = make_config(llm_provider="deepseek", deepseek_api_key="k", llm_model_extract="deepseek-chat")
    provider = provider_from_env(cfg)
    assert isinstance(provider, DeepSeekProvider)


def test_ollama_case_unchanged():
    cfg = make_config(llm_provider="ollama")
    assert isinstance(provider_from_env(cfg), OllamaProvider)
