"""Stage-facing batch boundary (PLAN.md Phase 1.8).

The manager is intentionally only an interface in this phase: providers still execute
their default batches sequentially and make the result available immediately. Owning
this boundary at worker scope lets M3.3 add collection, persistence, delayed polling,
and recovery without changing LLM-bearing stage call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio

from pipeline.llm.provider import (
    AdmissionRejected, BatchRequest, BatchResult, LLMProvider, RequestBudgetExceeded,
)


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


class RequestTooLarge(BatchError):
    """The provider rejected the normalized request size (safe to split)."""


def is_request_too_large(exc: BaseException) -> bool:
    """Classify only explicit context/request-size failures.

    A 429 is capacity backpressure and must bubble as ``AdmissionRejected`` so the
    caller waits. It is intentionally excluded even when a provider's message contains
    other generic wording. 413 and the common structured context-length code are the
    only split signals accepted here.
    """
    if isinstance(exc, AdmissionRejected):
        return False
    if isinstance(exc, RequestBudgetExceeded):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 413 or getattr(exc, "status_code", None) == 413:
        return True
    if getattr(exc, "provider_code", None) in {"context_length_exceeded", "request_too_large"}:
        return True
    return False


def classify_batch_error(exc: BaseException) -> str:
    """Return a bounded diagnostic class without retaining provider response text."""
    if isinstance(exc, AdmissionRejected):
        return "rate_limited" if getattr(exc, "category", "") == "rate_limited" else getattr(exc, "category", "admission_rejected")
    if is_request_too_large(exc):
        return "request_too_large"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "provider_error"


@dataclass(frozen=True)
class BatchManager:
    """Thin worker-owned adapter over an ``LLMProvider``'s batch methods."""

    provider: LLMProvider

    async def batch_submit(self, requests: list[BatchRequest]) -> str:
        return await self.provider.batch_submit(requests)

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        return await self.provider.batch_poll(batch_id)

    async def batch_submit_split(self, requests: list[BatchRequest], *, min_size: int = 1) -> list[str]:
        """Submit a batch, bisecting only an explicit normalized size rejection.

        The returned IDs preserve request order. AdmissionRejected (including 429) is
        re-raised untouched, so callers can coordinate a cooldown and retry the same
        complete batch. Cancellation is also allowed to propagate and never becomes a
        provider failure or a partial submission.
        """
        if min_size < 1:
            raise ValueError("min_size must be positive")
        if not requests:
            return []
        try:
            return [await self.batch_submit(requests)]
        except AdmissionRejected:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not is_request_too_large(exc):
                raise
            if len(requests) <= min_size:
                raise RequestTooLarge("provider rejected the smallest batch as too large") from exc
            midpoint = len(requests) // 2
            left = await self.batch_submit_split(requests[:midpoint], min_size=min_size)
            right = await self.batch_submit_split(requests[midpoint:], min_size=min_size)
            return left + right

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
