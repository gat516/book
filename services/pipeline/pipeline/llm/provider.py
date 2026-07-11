"""The ``LLMProvider`` protocol (instructions.md §5.4) and the default batch behaviour.

Every backend implements four async methods: ``complete`` (one prompt → text),
``embed`` (texts → vectors), and ``batch_submit``/``batch_poll`` (offline bulk work).
Providers with no native Batch API inherit ``SequentialBatchMixin``, which fulfils the
batch interface by just running ``complete`` one request at a time — so call sites can
use the batch shape from day one and gain real batching later without changing.
"""

from __future__ import annotations

import uuid
from typing import Protocol, TypedDict, runtime_checkable


class BatchRequest(TypedDict):
    id: str  # your idempotency key
    prompt: str
    system: str


class BatchResult(TypedDict):
    id: str
    output: str
    error: str | None


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
    ) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

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
                output = await self.complete(req["prompt"], system=req["system"])  # type: ignore[attr-defined]
                results.append({"id": req["id"], "output": output, "error": None})
            except Exception as exc:  # noqa: BLE001 — surface per-request, don't fail the batch
                results.append({"id": req["id"], "output": "", "error": str(exc)})
        batch_id = uuid.uuid4().hex
        self._batches[batch_id] = results
        return batch_id

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        return self._batches.get(batch_id, [])
