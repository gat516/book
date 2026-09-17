from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novel_llm import GeminiProvider, OpenRouterProvider, UnavailableEmbeddingProvider
from novel_llm.embedding_config import EmbeddingResolver, embedding_space


def config(**overrides):
    values = dict(embed_provider="auto", embed_model="nomic-embed-text", embed_dim=768,
                  gemini_api_key="", openrouter_api_key="", ollama_host="http://localhost:11434",
                  gemini_embed_base_url="https://generativelanguage.googleapis.com/v1beta",
                  openrouter_base_url="https://openrouter.ai/api/v1")
    return SimpleNamespace(**(values | overrides))


def connection(row=None):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=row))))


@pytest.mark.asyncio
async def test_auto_never_contacts_ollama_without_a_gemini_key():
    fallback = SimpleNamespace(embed=AsyncMock())
    resolver = EmbeddingResolver(config(), fallback, AsyncMock(return_value=(None, None)))
    binding = await resolver.resolve(connection())
    assert isinstance(binding.provider, UnavailableEmbeddingProvider)
    assert binding.space is None
    fallback.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_gemini_key_enables_auto_and_rotation_reuses_vector_space():
    credentials = AsyncMock(return_value=("https://ignored.example", "first-key"))
    resolver = EmbeddingResolver(config(), None, credentials)
    first = await resolver.resolve(connection())
    assert isinstance(first.provider, GeminiProvider)
    assert first.provider._api_key == "first-key"
    assert first.provider._embed_model == "gemini-embedding-001"
    assert (await resolver.resolve(connection())).provider is first.provider
    credentials.return_value = (None, "rotated-key")
    second = await resolver.resolve(connection())
    assert second.provider is not first.provider
    assert second.space == first.space
    credentials.return_value = (None, None)
    assert (await resolver.resolve(connection())).space is None
    await resolver.aclose()


@pytest.mark.asyncio
async def test_ui_hosted_choice_overrides_explicit_ollama_server_and_uses_saved_key():
    credentials = AsyncMock(return_value=(None, "router-key"))
    resolver = EmbeddingResolver(config(embed_provider="ollama"), object(), credentials)
    binding = await resolver.resolve(connection(("openrouter", "openai/text-embedding-3-small")))
    assert isinstance(binding.provider, OpenRouterProvider)
    assert binding.provider._embed_dim == 768
    assert binding.provider._api_key == "router-key"
    credentials.assert_awaited_once()
    await resolver.aclose()


@pytest.mark.asyncio
async def test_off_overrides_even_available_credentials_and_server_preserves_explicit_backend():
    fallback = object()
    credentials = AsyncMock(return_value=(None, "key"))
    resolver = EmbeddingResolver(config(embed_provider="ollama"), fallback, credentials)
    assert (await resolver.resolve(connection(("disabled", "")))).space is None
    credentials.assert_not_awaited()
    assert (await resolver.resolve(connection(("server", "")))).provider is fallback


@pytest.mark.asyncio
async def test_env_key_can_supply_auto_but_saved_key_wins():
    credentials = AsyncMock(return_value=(None, None))
    resolver = EmbeddingResolver(config(gemini_api_key="env-key"), None, credentials)
    assert (await resolver.resolve(connection())).provider._api_key == "env-key"
    credentials.return_value = (None, "saved-key")
    assert (await resolver.resolve(connection())).provider._api_key == "saved-key"
    await resolver.aclose()


def test_same_width_does_not_make_different_models_or_endpoints_compatible():
    original = embedding_space("gemini", "one", 768, "https://one")
    assert original != embedding_space("gemini", "two", 768, "https://one")
    assert original != embedding_space("gemini", "one", 768, "https://two")
    assert original != embedding_space("openrouter", "one", 768, "https://one")


@pytest.mark.asyncio
async def test_broken_optional_credential_does_not_block_other_ai_features(caplog):
    resolver = EmbeddingResolver(config(), None, AsyncMock(side_effect=RuntimeError("secret-not-for-logs")))
    binding = await resolver.resolve(connection())
    assert binding.space is None
    assert "secret-not-for-logs" not in caplog.text
