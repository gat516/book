"""Per-novel LLM provider resolution for askai (PLAN.md Phase N4).

Deliberate, small duplication of services/pipeline/pipeline/provider_config.py rather
than a shared dependency: askai is a separate deployable service (the sibling-project
boundary this repo already draws elsewhere), and the decrypt-and-build logic here is a
dozen lines, not worth a shared package for. Keep the two in sync by hand if the
encryption scheme ever changes.

Decryption uses the SAME key bytes ingest-api encrypted with
(``PROVIDER_CONFIG_ENCRYPTION_KEY``, base64, 32 bytes — must match ingest-api's
``INGEST_PROVIDER_CONFIG_KEY`` exactly).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from novel_llm import AnthropicProvider, DeepSeekProvider, GeminiProvider, LLMProvider, OllamaProvider
from novel_llm.gemini import DEFAULT_BASE_URL as GEMINI_BASE_URL


@dataclass(frozen=True)
class ProviderConfigRow:
    provider: str
    model: str | None
    base_url: str | None
    api_key: str | None


def _decryption_key() -> bytes:
    raw = os.environ.get("PROVIDER_CONFIG_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError(
            "a novel has a provider_config with an encrypted api_key, but "
            "PROVIDER_CONFIG_ENCRYPTION_KEY is not set"
        )
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError(f"PROVIDER_CONFIG_ENCRYPTION_KEY must decode to 32 bytes, got {len(key)}")
    return key


async def load_provider_config(conn, novel_id: str) -> ProviderConfigRow | None:
    row = await (
        await conn.execute(
            "SELECT provider, model, base_url, api_key_cipher, api_key_nonce "
            "FROM novel_provider_config WHERE novel_id = %s",
            (novel_id,),
        )
    ).fetchone()
    if row is None:
        return None
    provider, model, base_url, cipher, nonce = row
    api_key = None
    if cipher is not None:
        api_key = AESGCM(_decryption_key()).decrypt(bytes(nonce), bytes(cipher), None).decode("utf-8")
    return ProviderConfigRow(provider=provider, model=model, base_url=base_url, api_key=api_key)


def build_provider(row: ProviderConfigRow, *, default_model: str, ollama_host: str, deepseek_base_url: str) -> LLMProvider:
    match row.provider:
        case "ollama":
            return OllamaProvider(host=row.base_url or ollama_host, model=row.model or default_model)
        case "anthropic":
            return AnthropicProvider(model=row.model or default_model, api_key=row.api_key)
        case "gemini":
            # No gemini_base_url parameter here: the endpoint is a fixed Google URL, so the
            # provider's own default stands in rather than widening this signature.
            return GeminiProvider(
                model=row.model or default_model,
                base_url=row.base_url or GEMINI_BASE_URL,
                api_key=row.api_key,
            )
        case "deepseek":
            return DeepSeekProvider(
                model=row.model or default_model,
                base_url=row.base_url or deepseek_base_url,
                api_key=row.api_key,
            )
        case other:
            raise ValueError(f"unknown provider in novel_provider_config: {other!r}")
