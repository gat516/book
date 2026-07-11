"""Provider abstraction (instructions.md §5.4).

All LLM calls go through the ``LLMProvider`` protocol; no stage imports a provider SDK
directly. Backends are chosen from config here so switching Anthropic ↔ Ollama is one
env var (``LLM_PROVIDER``). The embedding backend is separate (§5.4): embeddings always
run through Ollama's ``nomic-embed-text`` regardless of the completion backend.
"""

from __future__ import annotations

from pipeline.config import Config
from pipeline.llm.anthropic import AnthropicProvider
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import LLMProvider


def provider_from_env(cfg: Config) -> LLMProvider:
    """Return the completion backend named by ``LLM_PROVIDER``."""
    match cfg.llm_provider:
        case "ollama":
            return OllamaProvider(host=cfg.ollama_host, model=cfg.llm_model_extract)
        case "anthropic":
            return AnthropicProvider(model=cfg.llm_model_extract)
        case other:
            raise ValueError(f"unknown LLM_PROVIDER: {other!r}")


def embed_provider_from_env(cfg: Config) -> LLMProvider:
    """Return the embedding backend — always Ollama ``nomic-embed-text`` (§5.4)."""
    return OllamaProvider(host=cfg.ollama_host, model=cfg.embed_model)


__all__ = ["LLMProvider", "provider_from_env", "embed_provider_from_env"]
