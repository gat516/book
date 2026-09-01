"""provider_from_env's backend selection (PLAN.md Phase N3: added the "deepseek" case)."""

from __future__ import annotations

from fixtures import make_config
from novel_llm import DeepSeekProvider, OllamaProvider
import pytest

from pipeline.config import names_runtime, resolve_runtime
from pipeline.llm import provider_from_env
from pipeline.provider_config import (
    ProviderConfigRow,
    build_names_provider,
    build_resolve_provider,
)


def test_deepseek_case_constructs_a_deepseek_provider():
    cfg = make_config(llm_provider="deepseek", deepseek_api_key="k", llm_model_extract="deepseek-chat")
    provider = provider_from_env(cfg)
    assert isinstance(provider, DeepSeekProvider)


def test_ollama_case_unchanged():
    cfg = make_config(llm_provider="ollama")
    assert isinstance(provider_from_env(cfg), OllamaProvider)


def test_names_provider_is_none_for_hosted_providers():
    """A novel pinned to a hosted provider must keep that routing (Phase N4).

    The CHARACTER_NAMES budget is expressed as OllamaProvider constructor kwargs, so
    honouring it means a second local client — which for an Anthropic/DeepSeek novel would
    silently send that novel's work somewhere it never asked for.
    """
    for provider_id in ("anthropic", "deepseek", "gateway"):
        assert build_names_provider(make_config(), provider_id=provider_id) is None


def test_names_provider_carries_the_stage_budget_and_streams():
    cfg = make_config(llm_provider="ollama", names_ollama_first_token_seconds=777,
                      names_ollama_timeout_seconds=88, names_ollama_total_timeout_seconds=999)
    provider = build_names_provider(cfg, provider_id="ollama")
    assert isinstance(provider, OllamaProvider)
    # Streaming is what splits prefill from the inter-token gap; without it the two-phase
    # budget collapses back to one flat read ceiling.
    assert provider._stream is True
    assert provider._first_token_timeout == 777
    assert provider._idle_timeout == 88
    assert provider._total_timeout == 999


def test_names_provider_honours_a_novels_own_ollama_host_and_model():
    row = ProviderConfigRow(provider="ollama", model="qwen2.5:7b-instruct",
                            base_url="http://elsewhere:11434", api_key=None)
    provider = build_names_provider(make_config(), provider_id="ollama", row=row)
    assert provider._model == "qwen2.5:7b-instruct"
    assert "elsewhere" in provider._host


def test_names_runtime_rejects_a_total_below_its_own_phases():
    """total_timeout is the outer bound; a smaller one would abort a call the phase
    budgets still consider healthy."""
    with pytest.raises(ValueError):
        names_runtime(make_config(names_ollama_first_token_seconds=900,
                                  names_ollama_total_timeout_seconds=60))
    with pytest.raises(ValueError):
        names_runtime(make_config(names_ollama_timeout_seconds=0))


def test_resolve_provider_carries_phase_budgets_and_streams():
    cfg = make_config(resolve_ollama_first_token_seconds=700,
                      resolve_ollama_timeout_seconds=45,
                      resolve_ollama_total_timeout_seconds=1200)
    provider = build_resolve_provider(cfg, provider_id="ollama")
    assert isinstance(provider, OllamaProvider)
    assert provider._stream is True
    assert provider._first_token_timeout == 700
    assert provider._idle_timeout == 45
    assert provider._total_timeout == 1200


def test_resolve_provider_does_not_replace_hosted_routing():
    for provider_id in ("anthropic", "deepseek", "gateway"):
        assert build_resolve_provider(make_config(), provider_id=provider_id) is None


def test_resolve_runtime_rejects_incoherent_limits():
    with pytest.raises(ValueError):
        resolve_runtime(make_config(resolve_ollama_first_token_seconds=900,
                                    resolve_ollama_total_timeout_seconds=60))
    with pytest.raises(ValueError):
        resolve_runtime(make_config(resolve_ollama_timeout_seconds=0))
