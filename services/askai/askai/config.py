from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    internal_token: str
    model: str
    embed_dim: int
    host: str
    port: int
    max_chunks: int = 8
    max_entities: int = 8
    max_facts: int = 64
    max_edges: int = 32
    max_context_chars: int = 48_000
    embed_model: str = "nomic-embed-text"
    # Process-wide provider defaults (PLAN.md Phase N4) — the fallback for a novel with
    # no novel_provider_config row. Mirrors pipeline/config.py's fields of the same name.
    llm_provider: str = "anthropic"
    ollama_host: str = "http://localhost:11434"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"


def load_config() -> Config:
    return Config(
        database_url=os.getenv("ASKAI_DATABASE_URL", os.getenv("READER_DATABASE_URL", os.getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"))),
        internal_token=os.getenv("ASKAI_INTERNAL_TOKEN", ""),
        model=os.getenv("LLM_MODEL_ASK", os.getenv("LLM_MODEL_EXTRACT", "")),
        embed_dim=int(os.getenv("EMBED_DIM", "768")),
        host=os.getenv("ASKAI_HOST", "0.0.0.0"),
        port=int(os.getenv("ASKAI_PORT", "8082")),
        embed_model=os.getenv("EMBED_MODEL", "nomic-embed-text"),
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
