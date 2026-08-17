"""Phase 1.8's worker-owned batch boundary and malformed-result tripwires."""

from __future__ import annotations

import pytest

from pipeline.batch import BatchManager, BatchProtocolError, BatchRequestFailed
from pipeline.llm.provider import BatchResult


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
