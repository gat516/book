"""SequentialBatchMixin semantics (§5.4): the default batch path runs complete() once per
request and returns one result per request, matched by id. Pure — no network.
"""

from __future__ import annotations

import pytest

from pipeline.llm.provider import BatchRequest, SequentialBatchMixin


class FakeProvider(SequentialBatchMixin):
    """A provider whose complete() echoes the prompt, for exercising the batch default."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False) -> str:
        self.calls += 1
        return f"echo:{prompt}"


async def test_batch_runs_complete_per_request_and_matches_by_id():
    p = FakeProvider()
    reqs: list[BatchRequest] = [
        {"id": "a", "prompt": "one", "system": ""},
        {"id": "b", "prompt": "two", "system": ""},
    ]
    batch_id = await p.batch_submit(reqs)
    results = await p.batch_poll(batch_id)

    assert p.calls == 2
    by_id = {r["id"]: r for r in results}
    assert by_id["a"]["output"] == "echo:one"
    assert by_id["b"]["output"] == "echo:two"
    assert all(r["error"] is None for r in results)


async def test_per_request_error_is_isolated():
    class Boom(SequentialBatchMixin):
        async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False) -> str:
            if prompt == "bad":
                raise RuntimeError("kaboom")
            return "ok"

    p = Boom()
    batch_id = await p.batch_submit(
        [
            {"id": "good", "prompt": "fine", "system": ""},
            {"id": "bad", "prompt": "bad", "system": ""},
        ]
    )
    results = {r["id"]: r for r in await p.batch_poll(batch_id)}
    assert results["good"]["error"] is None
    assert results["bad"]["error"] is not None and "kaboom" in results["bad"]["error"]


async def test_poll_unknown_batch_returns_empty():
    p = FakeProvider()
    assert await p.batch_poll("nope") == []
