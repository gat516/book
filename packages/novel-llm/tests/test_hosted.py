"""LiteLLM hosted-provider contract tests; every call is mocked and offline."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import novel_llm.hosted as hosted
from novel_llm.anthropic import AnthropicProvider
from novel_llm.deepseek import DeepSeekProvider
from novel_llm.gemini import GeminiProvider
from novel_llm.groq import GroqProvider
from novel_llm.provider import (
    AdmissionRejected,
    Class,
    PinnedModelChanged,
    RequestBudgetExceeded,
    TruncatedOutput,
    UnsupportedSchema,
)


def response(*, model="served-model", content="{}", finish_reason="stop", usage=None,
             provider=None, headers=None):
    hidden = {"custom_llm_provider": provider} if provider else {}
    if headers:
        hidden["additional_headers"] = headers
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(finish_reason=finish_reason,
                                 message=SimpleNamespace(content=content))],
        usage=usage or SimpleNamespace(prompt_tokens=11, completion_tokens=7,
                                       prompt_tokens_details=SimpleNamespace(cached_tokens=3)),
        _hidden_params=hidden,
    )


@pytest.mark.asyncio
async def test_groq_and_deepseek_share_litellm_contract(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return response(model="groq-model" if kwargs["model"].startswith("groq/") else "deepseek-model",
                        provider=kwargs["model"].split("/", 1)[0])

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    groq = GroqProvider(model="llama", api_key="groq-key")
    deepseek = DeepSeekProvider(model="deepseek-chat", api_key="deep-key")
    await groq.complete("source", cls=Class.INTERACTIVE)
    result = await deepseek.complete("source")
    assert result.served_provider == "deepseek"
    assert result.served_model == "deepseek-model"
    assert result.input_tokens == 11 and result.output_tokens == 7
    assert calls[0]["model"] == "groq/llama"
    assert calls[0]["api_key"] == "groq-key"
    assert calls[0]["num_retries"] == 0 and calls[0]["fallbacks"] == []
    assert calls[1]["model"] == "deepseek/deepseek-chat"
    await groq.aclose()
    await deepseek.aclose()


@pytest.mark.asyncio
async def test_schema_transport_budget_identity_and_rate_headers(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return response(model="claude", provider="anthropic",
                        headers={"x-ratelimit-limit-tokens": "1000",
                                 "x-ratelimit-remaining-tokens": "990",
                                 "authorization": "must-not-escape"})

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = AnthropicProvider(model="claude-haiku", api_key="anthropic-key",
                                 base_url="http://proxy.test")
    result = await provider.complete("source", system="stable",
                                    json_schema={"type": "object"}, max_output_tokens=37)
    assert calls[0]["model"] == "anthropic/claude-haiku"
    assert calls[0]["api_base"] == "http://proxy.test"
    assert calls[0]["max_tokens"] == 37
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert '"type": "object"' in calls[0]["messages"][0]["content"]
    assert result.rate_limits == {"x-ratelimit-limit-tokens": "1000",
                                  "x-ratelimit-remaining-tokens": "990"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return response(provider="deepseek")

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = DeepSeekProvider(model="chat")
    await provider.complete("source")
    assert calls[0]["api_key"] == "from-env"


@pytest.mark.asyncio
async def test_gemini_uses_shared_json_object_and_counts_thinking_tokens(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return response(model="gemini-2.5-flash", provider="gemini",
                        usage=SimpleNamespace(prompt_tokens=49, completion_tokens=66,
                                              total_tokens=507))

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = GeminiProvider(model="gemini-2.5-flash")
    result = await provider.complete("source", json_mode=True)
    assert calls[0]["model"] == "gemini/gemini-2.5-flash"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert result.served_provider == "gemini"
    assert result.output_tokens == 458


def test_litellm_import_is_forced_to_use_local_cost_map():
    """Importing the hosted adapter must never trigger LiteLLM's cost-map fetch."""
    source_root = Path(__file__).parents[1] / "src"
    env = os.environ.copy()
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    env["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-c",
         "import os; import novel_llm.hosted; print(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'])"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "True"
    assert "Failed to fetch remote model cost map" not in result.stdout
    assert "Failed to fetch remote model cost map" not in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(413, RequestBudgetExceeded), (429, AdmissionRejected)])
async def test_sdk_http_failures_are_normalized_without_retry(monkeypatch, status, expected):
    request = httpx.Request("POST", "https://provider.test")
    headers = {"retry-after": "42"} if status == 429 else {}
    http_response = httpx.Response(status, request=request, headers=headers, text="private source")

    class SDKError(Exception):
        def __init__(self):
            self.response = http_response

    async def complete(**kwargs):
        raise SDKError()

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = DeepSeekProvider(model="chat", api_key="key")
    with pytest.raises(expected) as caught:
        await provider.complete("source")
    if status == 429:
        assert caught.value.retry_after_s == 42
        assert caught.value.category == "rate_limited"
    await provider.aclose()


@pytest.mark.asyncio
async def test_schema_truncation_unsupported_schema_pin_and_cancellation(monkeypatch):
    class UnsupportedParamsError(Exception):
        pass

    async def unsupported(**kwargs):
        raise UnsupportedParamsError()

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=unsupported))
    provider = AnthropicProvider(model="claude", api_key="key")
    with pytest.raises(UnsupportedSchema):
        await provider.complete("source", json_schema={"type": "object"})

    async def truncated(**kwargs):
        return response(finish_reason="length")

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=truncated))
    with pytest.raises(TruncatedOutput):
        await provider.complete("source")

    async def changed(**kwargs):
        return response(model="other", provider="anthropic")

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=changed))
    with pytest.raises(PinnedModelChanged):
        await provider.complete("source", pin_model=True)

    async def cancelled(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=cancelled))
    with pytest.raises(asyncio.CancelledError):
        await provider.complete("source")
    await provider.aclose()
