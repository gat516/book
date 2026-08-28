"""Provider contracts shared by ingestion and reader-facing services."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import NotRequired, Protocol, TypedDict, runtime_checkable


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
