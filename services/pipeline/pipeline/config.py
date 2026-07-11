"""Configuration from the environment.

Mirrors ``services/ingest-api/config.go``: every value has a localhost default so the
worker runs against the compose stack with no setup. Model/provider settings come from
the same ``.env.example`` block ingest-api and the spec (§10) share.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _getenv(key: str, fallback: str) -> str:
    """Return the env var if set and non-empty, else the fallback (mirror of Go getenv)."""
    v = os.getenv(key)
    return v if v else fallback


@dataclass(frozen=True)
class Config:
    # Infra
    database_url: str
    redis_url: str
    object_endpoint: str  # bare host:port, no scheme (the minio client takes secure separately)
    object_access_key: str
    object_secret_key: str
    object_bucket: str
    object_secure: bool

    # LLM / models (§5.4, §10)
    llm_provider: str
    llm_model_translate: str
    llm_model_extract: str
    embed_model: str
    ollama_host: str

    # Cache-key inputs (§6.1) — bumping either invalidates the LLM-result cache.
    prompt_version: str
    config_version: str

    # Worker
    queue_timeout: int  # seconds BLMOVE blocks before looping (0 = block forever)

    @classmethod
    def load(cls) -> "Config":
        # OBJECT_STORE_ENDPOINT in .env.example is a URL (http://localhost:9000); the
        # minio client wants a bare host:port plus a secure flag, so split them here.
        raw_endpoint = _getenv("OBJECT_STORE_ENDPOINT", "http://localhost:9000")
        parsed = urlparse(raw_endpoint if "//" in raw_endpoint else f"//{raw_endpoint}")
        host = parsed.netloc or parsed.path  # netloc is empty when no scheme was given
        secure = parsed.scheme == "https" or os.getenv("OBJECT_STORE_USE_SSL") == "true"

        return cls(
            database_url=_getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"),
            redis_url=_getenv("REDIS_URL", "redis://localhost:6379"),
            object_endpoint=host,
            object_access_key=_getenv("OBJECT_STORE_ACCESS_KEY", "minio"),
            object_secret_key=_getenv("OBJECT_STORE_SECRET_KEY", "minio12345"),
            object_bucket=_getenv("OBJECT_STORE_BUCKET", "raw-chapters"),
            object_secure=secure,
            llm_provider=_getenv("LLM_PROVIDER", "ollama"),
            llm_model_translate=_getenv("LLM_MODEL_TRANSLATE", "qwen2.5:14b"),
            llm_model_extract=_getenv("LLM_MODEL_EXTRACT", "qwen2.5:14b"),
            embed_model=_getenv("EMBED_MODEL", "nomic-embed-text"),
            ollama_host=_getenv("OLLAMA_HOST", "http://localhost:11434"),
            prompt_version=_getenv("PROMPT_VERSION", "1"),
            config_version=_getenv("CONFIG_VERSION", "1"),
            queue_timeout=int(_getenv("PIPELINE_QUEUE_TIMEOUT", "5")),
        )
