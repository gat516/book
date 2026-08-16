"""The ``LLMProvider`` protocol (instructions.md §5.4) and the default batch behaviour.

Every backend implements four async methods: ``complete`` (one prompt → a ``Completion``),
``embed`` (texts → vectors), and ``batch_submit``/``batch_poll`` (offline bulk work).
Providers with no native Batch API inherit ``SequentialBatchMixin``, which fulfils the
batch interface by just running ``complete`` one request at a time — so call sites can
use the batch shape from day one and gain real batching later without changing.

``complete`` returns a ``Completion``, not a bare string, because the identity of what
answered is load-bearing: the LLM-result cache (§3.5, §6.1, jobs.py) must key on the
model that actually produced the output, never the model that was merely requested. A
future gateway integration (§14) can fail over to a different model transparently; a
provider here that only ever returns the requested model's identity is the honest
baseline that makes that later behavior visible instead of silently wrong.

``cls`` (§14.2) carries interactive/batch priority through every call. It is unused by
the direct-provider backends today — Ollama and Anthropic have no priority concept — but
threading it through now means adding a gateway backend later is a new class, not a
signature change at every call site.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypedDict, runtime_checkable


class Class(Enum):
    """Priority class for a call. See instructions.md §14.2, §14.4."""

    INTERACTIVE = "interactive"  # a reader is waiting (askai)
    BATCH = "batch"  # offline ingestion (pipeline stages)


@dataclass(frozen=True)
class Completion:
    """A completion AND the identity of what produced it.

    ``served_provider``/``served_model`` are NOT decoration — they are what the
    LLM-result cache keys on (jobs.py, §12). A direct-provider backend always echoes
    back what it was asked to use; only a router/gateway backend can legitimately
    report something different, and when one does, callers MUST treat that as
    information to act on, not metadata to ignore.
    """

    text: str
    served_provider: str
    served_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # tokens served from a provider prompt cache
    cache_write_tokens: int = 0  # tokens newly written to a provider prompt cache


class BatchRequest(TypedDict):
    id: str  # your idempotency key
    prompt: str
    system: str


class BatchResult(TypedDict):
    id: str
    output: str
    error: str | None
    served_provider: str
    served_model: str


class AdmissionRejected(Exception):
    """Backpressure, NOT job failure (instructions.md §6.2, §14.3).

    Raised when a call is refused for capacity reasons (a rate limiter, a gateway's
    admission control) rather than failing outright. Callers MUST retry this with
    backoff and MUST NOT count it toward ``job.attempts`` — conflating the two
    dead-letters the backlog under ordinary contention instead of just waiting it out.
    """

    def __init__(self, message: str = "admission rejected", *, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@runtime_checkable
class LLMProvider(Protocol):
    """``model`` lets a call override the provider's default model (e.g. the
    translate stage asking for the stronger translation model on a provider
    instance otherwise configured for cheap extraction). This is the fix for a
    real bug: constructing one provider instance per ``LLM_PROVIDER`` and letting
    every stage share it means whichever model built the instance is the only one
    that can ever actually run, while jobs.py computes a per-stage idempotency key
    that assumes otherwise — a translate job would be cached under a model that
    never produced its output. See jobs.py ``model_id_for_stage`` and llm/__init__.py.
    """

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
    ) -> Completion: ...

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]: ...

    async def batch_submit(self, requests: list[BatchRequest]) -> str: ...

    async def batch_poll(self, batch_id: str) -> list[BatchResult]: ...


class SequentialBatchMixin:
    """Default ``batch_*`` for providers without a native Batch API (§5.4).

    ``batch_submit`` runs every request through ``complete`` immediately, buffers the
    results under a locally generated id, and ``batch_poll`` returns them. The class that
    mixes this in must provide ``complete``.
    """

    def __init__(self) -> None:
        self._batches: dict[str, list[BatchResult]] = {}

    async def batch_submit(self, requests: list[BatchRequest]) -> str:
        results: list[BatchResult] = []
        for req in requests:
            try:
                completion = await self.complete(  # type: ignore[attr-defined]
                    req["prompt"], system=req["system"], cls=Class.BATCH
                )
                results.append(
                    {
                        "id": req["id"],
                        "output": completion.text,
                        "error": None,
                        "served_provider": completion.served_provider,
                        "served_model": completion.served_model,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — surface per-request, don't fail the batch
                results.append(
                    {
                        "id": req["id"],
                        "output": "",
                        "error": str(exc),
                        "served_provider": "",
                        "served_model": "",
                    }
                )
        batch_id = uuid.uuid4().hex
        self._batches[batch_id] = results
        return batch_id

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        return self._batches.get(batch_id, [])
