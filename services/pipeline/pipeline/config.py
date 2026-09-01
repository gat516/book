"""Configuration from the environment.

Mirrors ``services/ingest-api/config.go``: every value has a localhost default so the
worker runs against the compose stack with no setup. Model/provider settings come from
the same ``.env.example`` block ingest-api and the spec (§10) share.
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from urllib.parse import urlparse


def _getenv(key: str, fallback: str) -> str:
    """Return the env var if set and non-empty, else the fallback (mirror of Go getenv)."""
    v = os.getenv(key)
    return v if v else fallback


def _optional_float(key: str) -> float | None:
    """Like _getenv but with no fallback: unset stays None so the caller can refuse it.

    Graph inference budgets get this treatment because a wrong-but-plausible default is
    worse than no default — it fails minutes into a run, as an empty ReadTimeout.
    """
    v = os.getenv(key)
    return float(v) if v else None


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
    queue_timeout: int  # idle polling interval, bounded to 0.1–1s for ordered claims

    # Crash-recovery reaper (§6.3): renew a separate heartbeat during live work. Only
    # claims whose heartbeat is stale by visibility_timeout are recovered, regardless
    # of how long a healthy chapter takes to process.
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
    # Gemini speaks the OpenAI chat dialect through Google's compatibility endpoint,
    # so it needs only a key and a base -- no SDK, same shape as deepseek above.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Two distinct budgets, not one. graph_ollama_first_token_seconds bounds PREFILL —
    # Ollama's stream emits nothing at all while it processes the prompt, so this must
    # cover the whole prompt_eval phase. graph_ollama_timeout_seconds bounds the gap
    # BETWEEN tokens once generation is underway. Measured on this CPU-only host for a
    # 2656-token prompt: prefill 106s idle / 437s under load, inter-token gap ~0.3s.
    # Both default to None so an unset variable fails loudly in graph_runtime() instead
    # of inheriting a translate-sized timeout that has nothing to do with either phase.
    graph_ollama_first_token_seconds: float | None = None
    graph_ollama_timeout_seconds: float | None = None
    graph_ollama_total_timeout_seconds: float = 1800
    graph_ollama_num_ctx: int = 16384
    graph_ollama_num_predict: int = 4096

    # CHARACTER_NAMES gets the same two-phase treatment as graph inference, and for the
    # same measured reason: it batches up to 8 KB of source per call, so prefill dominates
    # on CPU-only hardware and a flat 120s budget aborts mid-prompt-eval. Unlike the graph
    # budgets these DEFAULT rather than staying None: graph rebuild is an operator-invoked
    # tool that can demand explicit values, but CHARACTER_NAMES sits on the ordinary ingest
    # path, where an unset variable must not break a working install on upgrade.
    names_ollama_first_token_seconds: float = 900
    names_ollama_timeout_seconds: float = 120
    names_ollama_total_timeout_seconds: float = 1800
    names_ollama_num_ctx: int = 16384

    # RESOLVE starts with one chapter-wide proposal and may follow it with several
    # per-surface decisions. On a CPU-only Ollama host, prompt evaluation alone can
    # exceed the ordinary 120s request timeout. Give it the same phase-aware streaming
    # budget as CHARACTER_NAMES so a healthy prefill is not mistaken for an outage.
    resolve_ollama_first_token_seconds: float = 900
    resolve_ollama_timeout_seconds: float = 120
    resolve_ollama_total_timeout_seconds: float = 1800
    resolve_ollama_num_ctx: int = 16384

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
            graph_ollama_first_token_seconds=_optional_float("GRAPH_OLLAMA_FIRST_TOKEN_SECONDS"),
            graph_ollama_timeout_seconds=_optional_float("GRAPH_OLLAMA_TIMEOUT_SECONDS"),
            graph_ollama_total_timeout_seconds=float(_getenv("GRAPH_OLLAMA_TOTAL_TIMEOUT_SECONDS", "1800")),
            graph_ollama_num_ctx=int(_getenv("GRAPH_OLLAMA_NUM_CTX", "16384")),
            graph_ollama_num_predict=int(_getenv("GRAPH_OLLAMA_NUM_PREDICT", "4096")),
            names_ollama_first_token_seconds=float(_getenv("NAMES_OLLAMA_FIRST_TOKEN_SECONDS", "900")),
            names_ollama_timeout_seconds=float(_getenv("NAMES_OLLAMA_TIMEOUT_SECONDS", "120")),
            names_ollama_total_timeout_seconds=float(_getenv("NAMES_OLLAMA_TOTAL_TIMEOUT_SECONDS", "1800")),
            names_ollama_num_ctx=int(_getenv("NAMES_OLLAMA_NUM_CTX", "16384")),
            resolve_ollama_first_token_seconds=float(_getenv("RESOLVE_OLLAMA_FIRST_TOKEN_SECONDS", "900")),
            resolve_ollama_timeout_seconds=float(_getenv("RESOLVE_OLLAMA_TIMEOUT_SECONDS", "120")),
            resolve_ollama_total_timeout_seconds=float(_getenv("RESOLVE_OLLAMA_TOTAL_TIMEOUT_SECONDS", "1800")),
            resolve_ollama_num_ctx=int(_getenv("RESOLVE_OLLAMA_NUM_CTX", "16384")),
            deepseek_api_key=_getenv("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=_getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            gateway_addr=_getenv("LLM_GATEWAY_ADDR", "localhost:8081"),
            gateway_backend=_getenv("LLM_GATEWAY_BACKEND", "local_gpu"),
            gateway_provider=_getenv("LLM_GATEWAY_PROVIDER", "ollama"),
            gateway_max_output_tokens=int(_getenv("LLM_GATEWAY_MAX_OUTPUT_TOKENS", "8192")),
            gemini_api_key=_getenv("GEMINI_API_KEY", ""),
            gemini_base_url=_getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
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


def graph_runtime(cfg: Config) -> dict:
    """Bounded runtime settings for graph inference, split by whether they affect output.

    ``identity`` changes what the model can produce and therefore belongs in the
    completion cache key. ``limits`` are HTTP/deadline budgets that cannot change a
    single generated token; folding them into cache identity would mean every timeout
    adjustment discarded hours of cached work on this hardware.
    """
    missing = [name for name, value in (('GRAPH_OLLAMA_FIRST_TOKEN_SECONDS', cfg.graph_ollama_first_token_seconds),
                                        ('GRAPH_OLLAMA_TIMEOUT_SECONDS', cfg.graph_ollama_timeout_seconds))
               if value is None]
    if missing:
        raise ValueError(
            f"graph inference budgets are unset: {', '.join(missing)}. Set them in .env and start "
            'through scripts/with-env.sh (make worker / make benchmark); a bare python -m invocation '
            'does not read .env. Prefill on CPU-only hardware here measured 106-437s for a '
            '2656-token prompt, so an inherited 120s budget aborts before the first token.')
    first_token, idle = cfg.graph_ollama_first_token_seconds, cfg.graph_ollama_timeout_seconds
    total = cfg.graph_ollama_total_timeout_seconds
    if not all(math.isfinite(v) and v > 0 for v in (first_token, idle, total)) or total < max(first_token, idle):
        raise ValueError('graph timeouts must be finite, positive, and total >= first-token and idle')
    if not 0 < cfg.graph_ollama_num_predict < cfg.graph_ollama_num_ctx:
        raise ValueError('graph output budget must be positive and smaller than context')
    return dict(
        identity=dict(version='stream-admission-v2', stream=True,
                      num_ctx=cfg.graph_ollama_num_ctx, num_predict=cfg.graph_ollama_num_predict),
        limits=dict(first_token_timeout_seconds=first_token, idle_timeout_seconds=idle,
                    total_timeout_seconds=total))


def names_runtime(cfg: Config) -> dict:
    """Bounded runtime settings for CHARACTER_NAMES inference, split like graph_runtime.

    Same split, same reason: ``identity`` can change what the model produces and so
    belongs in the completion cache key, while ``limits`` are deadlines that cannot alter
    a single generated token. Folding deadlines into identity would discard cached work
    every time a timeout was retuned on slow hardware.

    Unlike graph_runtime this never raises on unset values — the fields carry defaults,
    because this stage runs on the ordinary ingest path rather than behind an operator
    command. It still refuses values that are incoherent rather than merely absent.
    """
    first_token = cfg.names_ollama_first_token_seconds
    idle = cfg.names_ollama_timeout_seconds
    total = cfg.names_ollama_total_timeout_seconds
    if not all(math.isfinite(v) and v > 0 for v in (first_token, idle, total)) or total < max(first_token, idle):
        raise ValueError('character-name timeouts must be finite, positive, and total >= first-token and idle')
    if cfg.names_ollama_num_ctx <= 0:
        raise ValueError('character-name context budget must be positive')
    return dict(
        identity=dict(version='names-stream-v1', stream=True, num_ctx=cfg.names_ollama_num_ctx),
        limits=dict(first_token_timeout_seconds=first_token, idle_timeout_seconds=idle,
                    total_timeout_seconds=total))


def resolve_runtime(cfg: Config) -> dict:
    """Phase-aware Ollama limits for §5's identity-resolution model calls.

    These are operational deadlines, not output-shaping inputs, so changing them does
    not invalidate the job key. ``num_ctx`` and streaming mode can affect output and are
    kept in the identity half for the same reason as ``names_runtime``.
    """
    first_token = cfg.resolve_ollama_first_token_seconds
    idle = cfg.resolve_ollama_timeout_seconds
    total = cfg.resolve_ollama_total_timeout_seconds
    if not all(math.isfinite(v) and v > 0 for v in (first_token, idle, total)) or total < max(first_token, idle):
        raise ValueError('resolve timeouts must be finite, positive, and total >= first-token and idle')
    if cfg.resolve_ollama_num_ctx <= 0:
        raise ValueError('resolve context budget must be positive')
    return dict(
        identity=dict(version='resolve-stream-v1', stream=True, num_ctx=cfg.resolve_ollama_num_ctx),
        limits=dict(first_token_timeout_seconds=first_token, idle_timeout_seconds=idle,
                    total_timeout_seconds=total))
