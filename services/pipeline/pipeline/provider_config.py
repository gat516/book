"""Per-novel LLM provider resolution (PLAN.md Phase N4).

``novel_provider_config`` (migration 0011) holds an operator's optional per-novel
provider/model/API-key choice, written by ingest-api and encrypted there with
AES-GCM (services/ingest-api/crypto.go). This module is the read side: decrypt with
the SAME key bytes (``PROVIDER_CONFIG_ENCRYPTION_KEY``, base64, 32 bytes — coordinate
any rotation with ingest-api's ``INGEST_PROVIDER_CONFIG_KEY``) and construct the
matching ``LLMProvider``.

A novel with no row here is not an error — it's the common case (zero-config backward
compat): callers fall back to ``provider_from_env(cfg)``, the process-wide default.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from novel_llm import AnthropicProvider, DeepSeekProvider, LLMProvider, OllamaProvider
from pipeline.config import Config


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
        raise RuntimeError(
            f"PROVIDER_CONFIG_ENCRYPTION_KEY must decode to 32 bytes, got {len(key)}"
        )
    return key


async def load_provider_config(db, novel_id: str) -> ProviderConfigRow | None:
    """Fetch and decrypt novel_id's provider config, or None if it has none."""
    row = await (
        await db.execute(
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
        # AESGCM.decrypt raises on truncated/tampered ciphertext — deliberately not
        # caught here, since a novel with a corrupt provider_config should fail loudly
        # (raising out of _handle, chapter marked "error") rather than silently falling
        # back to the process default and translating under the wrong credentials.
        api_key = AESGCM(_decryption_key()).decrypt(bytes(nonce), bytes(cipher), None).decode("utf-8")
    return ProviderConfigRow(provider=provider, model=model, base_url=base_url, api_key=api_key)


def build_provider(row: ProviderConfigRow, cfg: Config) -> LLMProvider:
    """Construct the completion provider row describes, falling back to the service's
    own default model when the novel didn't override one."""
    match row.provider:
        case "ollama":
            return OllamaProvider(host=row.base_url or cfg.ollama_host, model=row.model or cfg.llm_model_extract)
        case "anthropic":
            return AnthropicProvider(model=row.model or cfg.llm_model_extract, api_key=row.api_key)
        case "deepseek":
            return DeepSeekProvider(
                model=row.model or cfg.llm_model_extract,
                base_url=row.base_url or cfg.deepseek_base_url,
                api_key=row.api_key,
            )
        case other:
            raise ValueError(f"unknown provider in novel_provider_config: {other!r}")
