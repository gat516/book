"""Provider contracts shared by ingestion and reader-facing services."""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

import httpx

# Statuses that mean "ask again later", not "this request is wrong". 429 is a rate limit,
# 5xx and 408 are the backend failing to serve a request that is itself valid.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class Class(Enum):
    INTERACTIVE = "interactive"
    BATCH = "batch"


@dataclass(frozen=True)
class Completion:
    text: str
    served_provider: str
    served_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Optional backend runtime diagnostics; never contain prompt/source text.
    timings: dict[str, float] = field(default_factory=dict)


class BatchRequest(TypedDict):
    id: str
    prompt: str
    system: str
    json_mode: NotRequired[bool]
    pin_model: NotRequired[bool]
    model: NotRequired[str | None]
    json_schema: NotRequired[dict | None]


class BatchResult(TypedDict):
    id: str
    output: str
    error: str | None
    served_provider: str
    served_model: str


class AdmissionRejected(Exception):
    """Capacity backpressure that callers must retry without counting as failure."""

    def __init__(self, message: str = "admission rejected", *, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
        json_schema: dict | None = None,
    ) -> Completion: ...

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]: ...

    async def batch_submit(self, requests: list[BatchRequest]) -> str: ...

    async def batch_poll(self, batch_id: str) -> list[BatchResult]: ...


class SequentialBatchMixin:
    """Sequential compatibility implementation for providers without native batches."""

    def __init__(self) -> None:
        self._batches: dict[str, list[BatchResult]] = {}

    async def batch_submit(self, requests: list[BatchRequest]) -> str:
        results: list[BatchResult] = []
        for req in requests:
            try:
                schema_options = (
                    {"json_schema": req["json_schema"]}
                    if req.get("json_schema") is not None else {}
                )
                completion = await self.complete(  # type: ignore[attr-defined]
                    req["prompt"], system=req["system"], json_mode=req.get("json_mode", False),
                    cls=Class.BATCH, pin_model=req.get("pin_model", False), model=req.get("model"),
                    **schema_options,
                )
                results.append({"id": req["id"], "output": completion.text, "error": None,
                                "served_provider": completion.served_provider, "served_model": completion.served_model})
            except AdmissionRejected:
                raise
            except Exception as exc:  # noqa: BLE001
                # str(exc) alone can be "" for exceptions raised with no message (seen
                # from httpx/json failures), which turned BatchRequestFailed's message
                # into "batch request '...' failed: " with zero diagnostic content.
                # Always include the exception type so a blank message is never silent.
                message = str(exc) or repr(exc)
                results.append({"id": req["id"], "output": "", "error": f"{type(exc).__name__}: {message}",
                                "served_provider": "", "served_model": ""})
        batch_id = uuid.uuid4().hex
        self._batches[batch_id] = results
        return batch_id

    async def batch_poll(self, batch_id: str) -> list[BatchResult]:
        return self._batches.get(batch_id, [])


def system_with_schema(system: str, schema: dict | None) -> str:
    """Prompt fallback for adapters without native schema support wired up yet.

    This is guidance, not constrained decoding; the caller must still validate.
    Keep the schema in the stable system prefix, never next to chapter text (§6.2).
    """
    if schema is None:
        return system
    return system + "\nReturn JSON matching this schema:\n" + json.dumps(schema, ensure_ascii=False)


@contextlib.asynccontextmanager
async def transient_as_backpressure(*, default_retry_s: float = 5.0):
    """Re-raise transient transport failures as AdmissionRejected.

    The distinction this draws is the one the worker already acts on. A malformed model
    response is a property of THIS chapter and should fail it; a rate limit, a 503, or a
    refused connection says nothing about the chapter at all, and failing it burns a retry
    from a budget meant for real problems. AdmissionRejected is the existing name for that
    second case ("retry without counting as failure"), so this maps onto it rather than
    inventing a parallel error path.

    Concretely: when Ollama was OOM-killed mid-run, five chapters died at once on
    ConnectError. Under this they requeue instead.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in TRANSIENT_STATUS:
            raise
        # Honour a server-supplied delay when there is one; a rate limiter knows better
        # than any constant we would pick.
        retry_after = exc.response.headers.get("retry-after", "")
        try:
            delay = float(retry_after)
        except ValueError:
            delay = default_retry_s
        raise AdmissionRejected(
            f"{exc.response.status_code} from provider", retry_after_s=max(delay, 0.0)
        ) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError) as exc:
        raise AdmissionRejected(f"provider unreachable: {type(exc).__name__}",
                                retry_after_s=default_retry_s) from exc
