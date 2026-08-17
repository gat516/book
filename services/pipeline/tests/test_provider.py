"""SequentialBatchMixin semantics (§5.4): the default batch path runs complete() once per
request and returns one result per request, matched by id. Pure — no network.

Also covers the served-model tripwire (§12, §14.3): a provider whose ``complete()``
reports a ``served_model`` different from what was requested must have that visible to
the caller through the ``Completion`` it returns, since that is the only signal that lets
jobs.py refuse to cache the result under the wrong key (jobs.cacheable_result).
"""

from __future__ import annotations

import pytest

from pipeline.jobs import cacheable_result
from pipeline.llm.provider import (
    AdmissionRejected,
    BatchRequest,
    Class,
    Completion,
    SequentialBatchMixin,
)


class FakeProvider(SequentialBatchMixin):
    """A provider whose complete() echoes the prompt, for exercising the batch default."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
    ) -> Completion:
        self.calls += 1
        return Completion(
            text=f"echo:{prompt}",
            served_provider="fake",
            served_model=model or "fake-default",
        )


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
    assert all(r["served_provider"] == "fake" for r in results)


async def test_batch_forwards_completion_options_at_batch_priority():
    class Capture(SequentialBatchMixin):
        def __init__(self) -> None:
            super().__init__()
            self.call: dict | None = None

        async def complete(
            self,
            prompt: str,
            *,
            system: str = "",
            json_mode: bool = False,
            cls: Class = Class.BATCH,
            pin_model: bool = False,
            model: str | None = None,
        ) -> Completion:
            self.call = {
                "prompt": prompt,
                "system": system,
                "json_mode": json_mode,
                "cls": cls,
                "pin_model": pin_model,
                "model": model,
            }
            return Completion(text="ok", served_provider="fake", served_model=model or "")

    provider = Capture()
    await provider.batch_submit(
        [
            {
                "id": "request",
                "prompt": "chapter",
                "system": "stable prefix",
                "json_mode": True,
                "pin_model": True,
                "model": "snapshot",
            }
        ]
    )

    assert provider.call == {
        "prompt": "chapter",
        "system": "stable prefix",
        "json_mode": True,
        "cls": Class.BATCH,
        "pin_model": True,
        "model": "snapshot",
    }


async def test_per_request_error_is_isolated():
    class Boom(SequentialBatchMixin):
        async def complete(
            self,
            prompt: str,
            *,
            system: str = "",
            json_mode: bool = False,
            cls: Class = Class.BATCH,
            pin_model: bool = False,
            model: str | None = None,
        ) -> Completion:
            if prompt == "bad":
                raise RuntimeError("kaboom")
            return Completion(text="ok", served_provider="fake", served_model="fake-default")

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


async def test_admission_rejection_escapes_batch_unchanged():
    rejected = AdmissionRejected("busy", retry_after_s=2.5)

    class Rejecting(SequentialBatchMixin):
        async def complete(
            self,
            prompt: str,
            *,
            system: str = "",
            json_mode: bool = False,
            cls: Class = Class.BATCH,
            pin_model: bool = False,
            model: str | None = None,
        ) -> Completion:
            raise rejected

    with pytest.raises(AdmissionRejected) as caught:
        await Rejecting().batch_submit([{"id": "request", "prompt": "hi", "system": ""}])

    assert caught.value is rejected
    assert caught.value.retry_after_s == 2.5


async def test_poll_unknown_batch_returns_empty():
    p = FakeProvider()
    assert await p.batch_poll("nope") == []


async def test_served_model_mismatch_is_visible_to_caller():
    """The provider reports what actually answered; a caller (jobs.py) decides what to
    do about a mismatch. This provider is asked for 'requested-model' but, standing in
    for a router/gateway that failed over, reports back 'other-model'."""

    class Failover(SequentialBatchMixin):
        async def complete(
            self,
            prompt: str,
            *,
            system: str = "",
            json_mode: bool = False,
            cls: Class = Class.BATCH,
            pin_model: bool = False,
            model: str | None = None,
        ) -> Completion:
            return Completion(text="ok", served_provider="fake", served_model="other-model")

    p = Failover()
    result = await p.complete("hi", model="requested-model")

    assert result.served_model == "other-model"
    assert not cacheable_result(
        "state",
        requested_model_id="fake:requested-model",
        served_provider=result.served_provider,
        served_model=result.served_model,
    )


async def test_matching_served_model_is_cacheable():
    result = Completion(text="ok", served_provider="ollama", served_model="qwen2.5:14b")
    assert cacheable_result(
        "state",
        requested_model_id="ollama:qwen2.5:14b",
        served_provider=result.served_provider,
        served_model=result.served_model,
    )
