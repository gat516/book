"""Provider abstraction (instructions.md §5.4).

All LLM calls go through the ``LLMProvider`` protocol; no stage imports a provider SDK
directly. Backends are chosen from config here so switching Anthropic ↔ Ollama is one
env var (``LLM_PROVIDER``). The embedding backend is separate (§5.4): ``EMBED_PROVIDER``
defaults to Ollama but can explicitly select a hosted embeddings API.

One provider instance is constructed with a *default* model (``llm_model_extract``), but
stages that need a different model (translate wants ``llm_model_translate``) MUST pass it
per-call via ``complete(..., model=jobs.model_for_stage(stage, cfg))`` rather than
constructing a second instance or assuming the default is right. ``jobs.model_for_stage``
is the single source of truth both here and for the idempotency key — see jobs.py for why
that used to be two sources of truth that could (and did) disagree.
"""

from __future__ import annotations

from pipeline.config import Config
from novel_llm import (
    AdmissionRejected,
    AnthropicProvider,
    Class,
    Completion,
    DeepSeekProvider,
    GatewayProvider,
    GeminiProvider,
    GroqProvider,
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
    UnavailableEmbeddingProvider,
)


def provider_from_env(cfg: Config, *, tenant: str = "default") -> LLMProvider:
    """Return the completion backend named by ``LLM_PROVIDER``, defaulted to the cheap
    extraction model. Callers needing a different model pass ``model=`` per call.

    This is the process-wide *fallback* used when a novel has no
    ``novel_provider_config`` row (PLAN.md Phase N4's zero-config backward compat) — the
    per-novel path constructs providers directly from decrypted config, not through here.
    """
    match cfg.llm_provider:
        case "ollama":
            return OllamaProvider(
                host=cfg.ollama_host, model=cfg.llm_model_extract, timeout=cfg.ollama_timeout_seconds
            )
        case "anthropic":
            return AnthropicProvider(model=cfg.llm_model_extract)
        case "deepseek":
            return DeepSeekProvider(
                model=cfg.llm_model_extract,
                base_url=cfg.deepseek_base_url,
                api_key=cfg.deepseek_api_key,
            )
        case "gemini":
            return GeminiProvider(
                model=cfg.llm_model_extract,
                base_url=cfg.gemini_base_url,
                api_key=cfg.gemini_api_key or None,
            )
        case "groq":
            return GroqProvider(model=cfg.llm_model_extract, base_url=cfg.groq_base_url,
                                api_key=cfg.groq_api_key or None)
        case "gateway":
            return GatewayProvider(address=cfg.gateway_addr, tenant=tenant,
                provider=cfg.gateway_provider, model=cfg.llm_model_extract,
                backend=cfg.gateway_backend, embed_model=cfg.embed_model,
                max_output_tokens=cfg.gateway_max_output_tokens)
        case other:
            raise ValueError(f"unknown LLM_PROVIDER: {other!r}")


def embed_provider_from_env(cfg: Config, *, tenant: str = "default") -> LLMProvider:
    """Return the independent retrieval embedding backend."""
    try:
        match cfg.embed_provider:
            case "ollama":
                return OllamaProvider(host=cfg.ollama_host, model=cfg.embed_model,
                                      timeout=cfg.ollama_timeout_seconds)
            case "openrouter":
                return OpenRouterProvider(
                    model=cfg.embed_model, embed_model=cfg.embed_model, embed_dim=cfg.embed_dim,
                    base_url=cfg.openrouter_base_url, api_key=cfg.openrouter_api_key or None,
                )
            case "gemini":
                return GeminiProvider(
                    model=cfg.embed_model, embed_model=cfg.embed_model, embed_dim=cfg.embed_dim,
                    base_url=cfg.gemini_base_url, embed_base_url=cfg.gemini_embed_base_url,
                    api_key=cfg.gemini_api_key or None,
                )
            case "gateway":
                return GatewayProvider(address=cfg.gateway_addr, tenant=tenant,
                    provider=cfg.gateway_provider, model=cfg.llm_model_extract,
                    backend=cfg.gateway_backend, embed_model=cfg.embed_model,
                    max_output_tokens=cfg.gateway_max_output_tokens)
            case other:
                raise ValueError(f"unknown EMBED_PROVIDER: {other!r}")
    except RuntimeError:
        # Missing hosted embedding credentials are retrieval-only degradation; worker
        # startup and chapter publication remain available with NULL vectors.
        if cfg.embed_provider in {"gemini", "openrouter"}:
            return UnavailableEmbeddingProvider()  # type: ignore[return-value]
        raise


__all__ = [
    "AdmissionRejected",
    "Class",
    "Completion",
    "LLMProvider",
    "provider_from_env",
    "embed_provider_from_env",
]
