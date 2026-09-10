"""Phase 1.8's worker-owned batch boundary and malformed-result tripwires."""

from __future__ import annotations

import asyncio
import pytest

from pipeline.batch import (BatchManager, BatchProtocolError, BatchRequestFailed,
                            RequestTooLarge, classify_batch_error)
from pipeline.llm.provider import BatchResult
from novel_llm import AdmissionRejected


def _result(request_id: str, *, error: str | None = None) -> BatchResult:
    return {
        "id": request_id,
        "output": "ok" if error is None else "",
        "error": error,
        "served_provider": "fake" if error is None else "",
        "served_model": "model" if error is None else "",
    }


def test_single_result_is_returned():
    result = _result("expected")
    assert BatchManager.require_single_result("expected", [result]) is result


async def test_manager_delegates_submit_and_poll():
    result = _result("expected")

    class Provider:
        def __init__(self) -> None:
            self.requests = None

        async def batch_submit(self, requests):
            self.requests = requests
            return "batch-id"

        async def batch_poll(self, batch_id):
            assert batch_id == "batch-id"
            return [result]

    provider = Provider()
    manager = BatchManager(provider)  # type: ignore[arg-type]
    request = {"id": "expected", "prompt": "chapter", "system": "stable"}

    batch_id = await manager.batch_submit([request])
    results = await manager.batch_poll(batch_id)

    assert provider.requests == [request]
    assert results == [result]


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([], "returned no result"),
        ([_result("expected"), _result("expected")], "returned 2 results"),
        ([_result("expected"), _result("other")], "unexpected result ids"),
    ],
)
def test_malformed_result_sets_are_rejected(results, message):
    with pytest.raises(BatchProtocolError, match=message):
        BatchManager.require_single_result("expected", results)


def test_request_error_is_raised_before_stage_persistence():
    with pytest.raises(BatchRequestFailed, match="provider exploded") as caught:
        BatchManager.require_single_result(
            "expected", [_result("expected", error="provider exploded")]
        )

    assert caught.value.request_id == "expected"
    assert caught.value.error == "provider exploded"


async def test_only_413_style_size_errors_split_and_preserve_all_request_ids():
    class Provider:
        def __init__(self):
            self.calls = []

        async def batch_submit(self, requests):
            self.calls.append([r["id"] for r in requests])
            if len(requests) > 1:
                response = httpx.Response(413, request=httpx.Request("POST", "http://x"))
                raise httpx.HTTPStatusError("request too large", request=response.request, response=response)
            return "batch-" + requests[0]["id"]

        async def batch_poll(self, _batch_id):
            return []

    import httpx
    provider = Provider()
    manager = BatchManager(provider)  # type: ignore[arg-type]
    requests = [{"id": str(i), "prompt": "x", "system": ""} for i in range(4)]
    ids = await manager.batch_submit_split(requests)
    assert ids == ["batch-0", "batch-1", "batch-2", "batch-3"]
    assert provider.calls == [["0", "1", "2", "3"], ["0", "1"], ["0"], ["1"], ["2", "3"], ["2"], ["3"]]


async def test_429_is_waitable_backpressure_and_is_never_split():
    class Provider:
        async def batch_submit(self, requests):
            raise AdmissionRejected("rate limited", retry_after_s=.01, category="rate_limited")

        async def batch_poll(self, _batch_id):
            return []

    manager = BatchManager(Provider())  # type: ignore[arg-type]
    with pytest.raises(AdmissionRejected):
        await manager.batch_submit_split([{"id": "a", "prompt": "x", "system": ""}])
    assert classify_batch_error(AdmissionRejected(category="rate_limited")) == "rate_limited"


async def test_split_cancellation_propagates_without_submitting_siblings():
    entered = asyncio.Event()

    class Provider:
        async def batch_submit(self, _requests):
            entered.set()
            await asyncio.sleep(60)

        async def batch_poll(self, _batch_id):
            return []

    manager = BatchManager(Provider())  # type: ignore[arg-type]
    task = asyncio.create_task(manager.batch_submit_split([
        {"id": "a", "prompt": "x", "system": ""}, {"id": "b", "prompt": "x", "system": ""}
    ]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
