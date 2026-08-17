"""Stage-facing batch boundary (PLAN.md Phase 1.8).

The manager is intentionally only an interface in this phase: providers still execute
their default batches sequentially and make the result available immediately. Owning
this boundary at worker scope lets M3.3 add collection, persistence, delayed polling,
and recovery without changing LLM-bearing stage call sites.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.llm.provider import BatchRequest, BatchResult, LLMProvider


class BatchError(RuntimeError):
    """Base class for failures observed through the stage-facing batch boundary."""


class BatchProtocolError(BatchError):
    """The provider returned a malformed result set for a submitted request."""


class BatchRequestFailed(BatchError):
    """A provider completed the batch but failed this individual request."""

    def __init__(self, request_id: str, error: str) -> None:
        super().__init__(f"batch request {request_id!r} failed: {error}")
        self.request_id = request_id
        self.error = error


@dataclass(frozen=True)
class BatchManager:
    """Thin worker-owned adapter over an ``LLMProvider``'s batch methods."""

    provider: LLMProvider

    async def batch_submit(self, requests: list[BatchRequest]) -> str:
        return await self.provider.batch_submit(requests)

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        return await self.provider.batch_poll(batch_id)

    @staticmethod
    def require_single_result(request_id: str, results: list[BatchResult]) -> BatchResult:
        """Return the sole expected result or fail before a stage can persist output."""
        unexpected = [result["id"] for result in results if result["id"] != request_id]
        if unexpected:
            raise BatchProtocolError(
                f"batch request {request_id!r} received unexpected result ids {unexpected!r}"
            )

        matching = [result for result in results if result["id"] == request_id]
        if not matching:
            raise BatchProtocolError(f"batch request {request_id!r} returned no result")
        if len(matching) > 1:
            raise BatchProtocolError(
                f"batch request {request_id!r} returned {len(matching)} results"
            )

        result = matching[0]
        if result["error"] is not None:
            raise BatchRequestFailed(request_id, result["error"])
        return result
