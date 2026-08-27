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
    embed_dim: int
    ollama_host: str
    ollama_timeout_seconds: float
    deepseek_api_key: str
    deepseek_base_url: str

    # How many DISTINCT chapters must independently propose the same source→target mapping
    # before RESOLVE locks it (migration 0016). Glossary rows are immutable and enforced
    # against every later translation, so a single hallucinated term is unrecoverable —
    # requiring the model to agree with itself across chapters is what keeps one bad call
    # from poisoning a novel. 1 restores lock-on-first-sight.
    glossary_min_proposals: int

    # Cache-key inputs (§6.1) — bumping either invalidates the LLM-result cache.
    prompt_version: str
    config_version: str

    # Worker
    queue_timeout: int  # seconds BLMOVE blocks before looping (0 = block forever)

    # Crash-recovery reaper (§6.3): a claimed job stranded in jobs:processing longer than
    # visibility_timeout is assumed crashed and requeued. Default ~3x a generous stage
    # estimate; retune once real LLM-bearing stages exist and p99.9 stage time is known.
    # A future gateway's admission lease_timeout must stay BELOW this value (§14.5) —
    # otherwise a job is requeued while its gateway reservation is still held and the
    # same work gets admitted twice.
    visibility_timeout: int
    reaper_interval: int  # seconds between reaper sweeps

    # Mention scanning. Python is the explicit standalone-development fallback; the
    # compose deployment sets this to grpc and talks to the Rust service.
    textproc_backend: str
    textproc_grpc_addr: str
    textproc_timeout_seconds: float
    gateway_addr: str = "localhost:8081"
    gateway_backend: str = "local_gpu"
    gateway_provider: str = "ollama"
    gateway_max_output_tokens: int = 8192

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
            embed_dim=int(_getenv("EMBED_DIM", "768")),
            ollama_host=_getenv("OLLAMA_HOST", "http://localhost:11434"),
            # A full-chapter TRANSLATE call is a much bigger prompt than the per-surface
            # RESOLVE calls, so on slow/local hardware it can outlast OllamaProvider's own
            # 120s httpx default well before the pipeline's own visibility_timeout would
            # ever matter. Independent knob, not derived from visibility_timeout: this is
            # an HTTP client timeout (one call), that's a crash-recovery window (whole
            # chapter, several calls).
            ollama_timeout_seconds=float(_getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
            deepseek_api_key=_getenv("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=_getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            gateway_addr=_getenv("LLM_GATEWAY_ADDR", "localhost:8081"),
            gateway_backend=_getenv("LLM_GATEWAY_BACKEND", "local_gpu"),
            gateway_provider=_getenv("LLM_GATEWAY_PROVIDER", "ollama"),
            gateway_max_output_tokens=int(_getenv("LLM_GATEWAY_MAX_OUTPUT_TOKENS", "8192")),
            glossary_min_proposals=int(_getenv("GLOSSARY_MIN_PROPOSALS", "2")),
            prompt_version=_getenv("PROMPT_VERSION", "1"),
            config_version=_getenv("CONFIG_VERSION", "1"),
            queue_timeout=int(_getenv("PIPELINE_QUEUE_TIMEOUT", "5")),
            visibility_timeout=int(_getenv("PIPELINE_VISIBILITY_TIMEOUT", "300")),
            reaper_interval=int(_getenv("PIPELINE_REAPER_INTERVAL", "5")),
            textproc_backend=_getenv("TEXTPROC_BACKEND", "python"),
            textproc_grpc_addr=_getenv("TEXTPROC_GRPC_ADDR", "localhost:50051"),
            textproc_timeout_seconds=float(_getenv("TEXTPROC_TIMEOUT_SECONDS", "10")),
        )
