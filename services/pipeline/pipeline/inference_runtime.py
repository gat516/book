"""Read-only local inference preflight. Never loads a model or generates tokens."""
from pathlib import Path
import asyncio
import hashlib
import math
import time
import os
from urllib.parse import urlsplit

import httpx

from novel_llm.admission import lock_path


def runtime_identity(*, output_tokens: int, schema_transport: str = "native",
                     schema_digest: str | None = None,
                     context_tokens: int | None = None) -> dict:
    """Return output-affecting request identity for cache/revision callers.

    Output headroom, context segmentation, and schema transport change model behavior
    and therefore belong in cache identity. Deadline/concurrency settings do not. This
    helper is intentionally pure so old revision records can keep their existing
    identity until callers opt in.
    """
    if output_tokens <= 0:
        raise ValueError("output_tokens must be positive")
    if context_tokens is not None and context_tokens <= 0:
        raise ValueError("context_tokens must be positive")
    if schema_transport not in {"native", "prompt", "duplicated"}:
        raise ValueError(f"unknown schema transport {schema_transport!r}")
    identity = {"output_tokens": output_tokens, "schema_transport": schema_transport}
    if schema_digest:
        identity["schema_digest"] = schema_digest
    if context_tokens is not None:
        identity["context_tokens"] = context_tokens
    return identity


def effective_schema_transport(provider, model: str | None) -> str:
    """Return the wire schema mode selected for this provider/model call.

    Some hosted adapters support native schemas only for a subset of models (Groq's
    GPT-OSS models are the current example).  Packing and cache identity must use this
    same decision rather than the provider's process-wide default.
    """
    selector = getattr(provider, "schema_transport", None)
    if callable(selector):
        selected = selector(model)
        if selected in {"native", "prompt"}:
            return selected
    # Compatibility for lightweight/legacy test doubles. Production adapters expose
    # schema_transport() and own all model-specific capability decisions.
    return "native" if bool(getattr(provider, "_native_json_schema", False)) else "prompt"


def credential_fingerprint(provider: str, base_url: str, credential: str) -> str:
    """Non-reversible tenant key for hosted cooldown coordination."""
    parsed = urlsplit(base_url)
    stable_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    return hashlib.sha256(f"{provider}\x1f{stable_url}\x1f{credential}".encode()).hexdigest()[:32]


class HostedCooldown:
    """Optional Redis-backed cooldown shared by processes using one hosted key.

    ``redis`` only needs async ``get``/``set``/``pttl`` methods. A missing Redis client
    degrades to process-local state, while a shared client coordinates all workers. The
    credential itself is never retained or emitted; only a one-way fingerprint appears
    in the Redis key. ``wait`` uses short sleeps so task cancellation is immediate.
    """

    def __init__(self, redis: object | None = None, *, provider: str = "",
                 base_url: str = "", credential: str = "", poll_seconds: float = 1.0):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.redis = redis
        self.provider = provider
        self.base_url = base_url
        self.poll_seconds = poll_seconds
        self._local_until = 0.0
        # Keep only the one-way fingerprint.  In particular, a long-lived cooldown
        # object must not retain the provider credential after deriving its key.
        self._key = "llm:cooldown:" + credential_fingerprint(provider, base_url, credential)

    async def note(self, retry_after_s: float) -> None:
        """Publish a server supplied cooldown, retaining the longer existing value."""
        delay = max(0.0, float(retry_after_s))
        if not delay:
            return
        local_until = time.monotonic() + delay
        self._local_until = max(self._local_until, local_until)
        if self.redis is None:
            return
        ttl_ms = max(1, math.ceil(delay * 1000))
        # Atomically retain the larger TTL when the Redis client supports EVAL. This
        # avoids a race where two workers both observe a short value and one shortens a
        # longer hint written by the other.
        script = (
            "local current = redis.call('PTTL', KEYS[1]); "
            "if current < tonumber(ARGV[1]) then "
            "redis.call('SET', KEYS[1], '1', 'PX', ARGV[1]); return 1; "
            "end; return 0"
        )
        eval_method = getattr(self.redis, "eval", None)
        if eval_method is not None:
            try:
                await eval_method(script, 1, self._key, str(ttl_ms))
                return
            except (TypeError, NotImplementedError):
                pass
            except Exception:
                return
        # Test doubles and small Redis-compatible clients may not expose EVAL. NX
        # followed by PTTL is a safe fallback for those clients.
        set_method = getattr(self.redis, "set")
        try:
            created = await set_method(self._key, "1", px=ttl_ms, nx=True)
        except TypeError:
            created = False
        except Exception:
            # Cooldown coordination is an optimization/safety valve, not a hosted
            # provider dependency. Keep the process-local deadline when Redis is down.
            return
        if created is False:
            try:
                if await self._pttl() < ttl_ms:
                    await set_method(self._key, "1", px=ttl_ms)
            except Exception:
                return

    async def _pttl(self) -> int:
        if self.redis is None:
            return max(0, math.ceil((self._local_until - time.monotonic()) * 1000))
        pttl = getattr(self.redis, "pttl", None)
        if pttl is not None:
            value = await pttl(self._key)
            return max(0, int(value))
        ttl = await getattr(self.redis, "ttl")(self._key)
        return max(0, int(float(ttl) * 1000))

    async def wait(self) -> float:
        """Wait until the shared cooldown expires; return seconds waited."""
        started = time.monotonic()
        while True:
            try:
                remaining_ms = await self._pttl()
            except Exception:
                remaining_ms = 0
            if remaining_ms <= 0:
                local_remaining = self._local_until - time.monotonic()
                if local_remaining <= 0:
                    return time.monotonic() - started
                remaining = local_remaining
            else:
                remaining = remaining_ms / 1000
            await asyncio.sleep(min(self.poll_seconds, max(0.001, remaining)))


class CooldownProvider:
    """Provider seam that coordinates hosted complete/batch admissions via Redis."""

    def __init__(self, provider, cooldown: HostedCooldown, *, owns_redis: bool = False):
        self._provider = provider
        self._cooldown = cooldown
        self._owns_redis = owns_redis

    def __getattr__(self, name):
        return getattr(self._provider, name)

    async def complete(self, *args, **kwargs):
        await self._cooldown.wait()
        try:
            return await self._provider.complete(*args, **kwargs)
        except Exception as exc:
            from pipeline.llm.provider import AdmissionRejected
            if isinstance(exc, AdmissionRejected):
                await self._cooldown.note(exc.retry_after_s)
            raise

    async def batch_submit(self, requests):
        await self._cooldown.wait()
        try:
            return await self._provider.batch_submit(requests)
        except Exception as exc:
            from pipeline.llm.provider import AdmissionRejected
            if isinstance(exc, AdmissionRejected):
                await self._cooldown.note(exc.retry_after_s)
            raise

    async def batch_poll(self, batch_id):
        return await self._provider.batch_poll(batch_id)

    async def embed(self, *args, **kwargs):
        return await self._provider.embed(*args, **kwargs)

    async def aclose(self):
        await self._provider.aclose()
        if self._owns_redis and self._cooldown.redis is not None:
            close = getattr(self._cooldown.redis, "aclose", None) or getattr(self._cooldown.redis, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result


class ResourceClosingProvider:
    """Delegate to a provider while closing an owned auxiliary resource."""

    def __init__(self, provider, resource):
        self._provider = provider
        self._resource = resource

    def __getattr__(self, name):
        return getattr(self._provider, name)

    async def aclose(self):
        await self._provider.aclose()
        close = getattr(self._resource, "aclose", None) or getattr(self._resource, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def coordinated_provider(provider, redis, *, provider_id: str, base_url: str = "",
                          credential: str = "", owns_redis: bool = False):
    """Wrap a hosted provider when a shared Redis coordinator can be formed."""
    if redis is None or provider_id not in {"anthropic", "deepseek", "gemini", "groq"}:
        return provider
    base_url = getattr(provider, "_base_url", None) or base_url
    credential = getattr(provider, "_api_key", None) or credential
    client = getattr(provider, "_client", None)
    if client is not None and not credential:
        headers = getattr(client, "headers", {})
        authorization = headers.get("authorization", "") if headers else ""
        credential = authorization.removeprefix("Bearer ").strip()
    if client is not None and not base_url:
        base_url = str(getattr(client, "base_url", base_url))
    if not credential:
        if owns_redis:
            return ResourceClosingProvider(provider, redis)
        return provider
    cooldown = HostedCooldown(redis, provider=provider_id, base_url=str(base_url),
                              credential=str(credential))
    return CooldownProvider(provider, cooldown, owns_redis=owns_redis)


async def preflight(cfg, model):
    # Keep this module importable from knowledge.py without graph_rebuild's reverse
    # import cycle; preflight is the only caller that needs the model probe.
    from pipeline.graph_rebuild import local_model
    identity = await local_model(cfg, model)  # also enforces installed, loopback-only
    async with httpx.AsyncClient(base_url=cfg.ollama_host, timeout=10) as client:
        version = await client.get('/api/version')
        version.raise_for_status()
        loaded = await client.get('/api/ps')
        loaded.raise_for_status()
    models = [dict(name=m.get('name'),size=m.get('size'),size_vram=m.get('size_vram'),
                   context_length=m.get('context_length')) for m in loaded.json()['models']]
    memory = {}
    if Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in {'MemTotal','MemAvailable','SwapTotal','SwapFree'}:
                memory[key + '_bytes'] = int(value.split()[0]) * 1024
    return dict(model=identity,ollama_version=version.json()['version'],loaded_models=models,
                logical_cpus=os.cpu_count(),memory=memory,admission_lock=str(lock_path(cfg.ollama_host)),
                inference_started=False,warnings=[
                    'Reservation covers updated Book processes sharing this lock directory, not external Ollama clients.',
                    'Restart existing worker and Ask AI processes after deploying admission changes.',
                    *(['Loaded models currently report CPU-only inference.'] if models and all(m['size_vram']==0 for m in models) else []),
                    *(['Multiple models are resident; an exclusive request does not evict idle models.'] if len(models)>1 else []),
                ])
