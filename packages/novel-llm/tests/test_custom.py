"""Custom OpenAI-compatible provider contract; all transport is mocked offline."""

from types import SimpleNamespace

import pytest

import novel_llm.hosted as hosted
from novel_llm.custom import CustomProvider
from novel_llm.provider import PinnedModelChanged


def response(model: str):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        _hidden_params={"custom_llm_provider": "openai"},
    )


@pytest.mark.asyncio
async def test_custom_provider_uses_openai_wire_and_preserves_custom_identity(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return response("model-a")

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = CustomProvider(
        model="model-a", base_url="https://models.example/v1", api_key="secret")
    completion = await provider.complete("hello", pin_model=True)

    assert calls[0]["model"] == "openai/model-a"
    assert calls[0]["api_base"] == "https://models.example/v1"
    assert calls[0]["api_key"] == "secret"
    assert completion.served_provider == "custom"
    assert completion.served_model == "model-a"


@pytest.mark.asyncio
async def test_custom_provider_still_rejects_a_changed_pinned_model(monkeypatch):
    async def complete(**_kwargs):
        return response("different-model")

    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    provider = CustomProvider(
        model="model-a", base_url="https://models.example/v1", api_key="secret")
    with pytest.raises(PinnedModelChanged):
        await provider.complete("hello", pin_model=True)
