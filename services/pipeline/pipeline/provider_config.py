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

from novel_llm import AnthropicProvider, DeepSeekProvider, GeminiProvider, LLMProvider, OllamaProvider
from pipeline.config import Config, names_runtime, resolve_runtime


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


async def load_provider_credential(db, provider: str) -> tuple[str | None, str | None]:
    """Fetch and decrypt the account-wide (base_url, api_key) for one provider (0035).

    Returns (None, None) when no credential is stored, which is an ordinary state: a novel
    may carry its own key, or the provider may need none at all (Ollama).
    """
    row = await (
        await db.execute(
            "SELECT base_url, api_key_cipher, api_key_nonce FROM provider_credential WHERE provider = %s",
            (provider,),
        )
    ).fetchone()
    if row is None:
        return None, None
    base_url, cipher, nonce = row
    api_key = None
    if cipher is not None:
        api_key = AESGCM(_decryption_key()).decrypt(bytes(nonce), bytes(cipher), None).decode("utf-8")
    return base_url, api_key


async def resolve_provider_config(db, novel_id: str, default_provider: str) -> ProviderConfigRow | None:
    """The novel's effective provider config, merging its own row over the global credential.

    Resolution, per field: the novel's value wins if it has one, else the account-wide
    credential for whichever provider is in play. That makes the common case -- one key in
    Settings, each book choosing only a model -- work without re-pasting the secret, while
    a book billed to a different account can still override with its own key.

    The provider itself comes from the novel's row when it has one, else the process-wide
    default, so a novel that has never been configured still picks up a global key.

    Returns None only when there is nothing to say beyond the process default, which is
    what keeps a zero-config install working exactly as before (Phase N4).
    """
    row = await load_provider_config(db, novel_id)
    provider = row.provider if row is not None else default_provider
    global_base_url, global_api_key = await load_provider_credential(db, provider)
    if row is None and global_api_key is None and global_base_url is None:
        return None
    return ProviderConfigRow(
        provider=provider,
        model=row.model if row is not None else None,
        base_url=(row.base_url if row is not None else None) or global_base_url,
        api_key=(row.api_key if row is not None else None) or global_api_key,
    )


def build_provider(row: ProviderConfigRow, cfg: Config) -> LLMProvider:
    """Construct the completion provider row describes, falling back to the service's
    own default model when the novel didn't override one."""
    match row.provider:
        case "ollama":
            return OllamaProvider(
                host=row.base_url or cfg.ollama_host,
                model=row.model or cfg.llm_model_extract,
                timeout=cfg.ollama_timeout_seconds,
            )
        case "anthropic":
            return AnthropicProvider(model=row.model or cfg.llm_model_extract, api_key=row.api_key)
        case "gemini":
            return GeminiProvider(
                model=row.model or cfg.llm_model_extract,
                base_url=row.base_url or cfg.gemini_base_url,
                api_key=row.api_key,
            )
        case "deepseek":
            return DeepSeekProvider(
                model=row.model or cfg.llm_model_extract,
                base_url=row.base_url or cfg.deepseek_base_url,
                api_key=row.api_key,
            )
        case other:
            raise ValueError(f"unknown provider in novel_provider_config: {other!r}")


def build_names_provider(cfg: Config, *, provider_id: str,
                         row: ProviderConfigRow | None = None) -> LLMProvider | None:
    """Dedicated CHARACTER_NAMES provider carrying that stage's own deadline budget.

    Returns None for every non-Ollama backend. The budget is expressed as OllamaProvider
    constructor kwargs — there is no per-call deadline anywhere in the LLMProvider
    protocol — so honouring it means standing up a separate instance, the same way
    KnowledgeEngine does for graph inference. Swapping a novel's pinned hosted provider
    for a local Ollama client to gain that budget would silently defeat
    novel_provider_config routing (Phase N4), so a hosted novel keeps ctx.provider and its
    ordinary timeout instead.

    ``stream=True`` is what actually unlocks the two-phase budget: only the streaming path
    distinguishes the prefill wait from the gap between tokens. It does not stream to the
    reader — the translation preview sink stays scoped to TRANSLATE.
    """
    if provider_id != "ollama":
        return None
    runtime = names_runtime(cfg)
    limits, identity = runtime["limits"], runtime["identity"]
    return OllamaProvider(
        host=(row.base_url if row else None) or cfg.ollama_host,
        model=(row.model if row else None) or cfg.llm_model_extract,
        timeout=limits["idle_timeout_seconds"],
        total_timeout=limits["total_timeout_seconds"],
        first_token_timeout=limits["first_token_timeout_seconds"],
        num_ctx=identity["num_ctx"],
        stream=identity["stream"],
    )


def build_resolve_provider(cfg: Config, *, provider_id: str,
                           row: ProviderConfigRow | None = None) -> LLMProvider | None:
    """Dedicated RESOLVE provider with CPU-appropriate phase deadlines (§5.4).

    As with ``build_names_provider``, hosted backends keep their configured routing and
    return None. Only an Ollama-backed novel gets a second client with streaming enabled;
    streaming is internal here and never reaches the reader-facing translation preview.
    """
    if provider_id != "ollama":
        return None
    runtime = resolve_runtime(cfg)
    limits, identity = runtime["limits"], runtime["identity"]
    return OllamaProvider(
        host=(row.base_url if row else None) or cfg.ollama_host,
        model=(row.model if row else None) or cfg.llm_model_extract,
        timeout=limits["idle_timeout_seconds"],
        total_timeout=limits["total_timeout_seconds"],
        first_token_timeout=limits["first_token_timeout_seconds"],
        num_ctx=identity["num_ctx"],
        stream=identity["stream"],
    )
