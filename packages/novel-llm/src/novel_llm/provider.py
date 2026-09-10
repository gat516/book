"""Provider contracts shared by ingestion and reader-facing services."""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

import httpx

# Statuses that mean "ask again later", not "this request is wrong". 429 is a rate limit,
# 5xx and 408 are the backend failing to serve a request that is itself valid.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
ADMISSION_CATEGORIES = frozenset({
    "rate_limited", "quota_exhausted", "provider_retry_exhausted", "unreachable", "embed_unavailable",
    "model_server_error",
})


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
    # A small allow-list of provider rate-limit headers from successful responses.
    # Arbitrary response headers can contain credentials or request material.
    rate_limits: dict[str, str] = field(default_factory=dict)


class BatchRequest(TypedDict):
    id: str
    prompt: str
    system: str
    json_mode: NotRequired[bool]
    pin_model: NotRequired[bool]
    model: NotRequired[str | None]
    json_schema: NotRequired[dict | None]
    max_output_tokens: NotRequired[int | None]


class BatchResult(TypedDict):
    id: str
    output: str
    error: str | None
    served_provider: str
    served_model: str


class AdmissionRejected(Exception):
    """Capacity backpressure that callers must retry without counting as failure."""

    def __init__(self, message: str = "admission rejected", *, retry_after_s: float = 0.0,
                 exact_hint: bool = False, category: str | None = None,
                 rate_limits: dict[str, str] | None = None,
                 rate_limit_details: dict[str, int | float] | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        # True when retry_after_s came from the provider itself rather than a local
        # default, so callers know it is an instruction to obey rather than a guess to
        # escalate from.
        self.exact_hint = exact_hint
        self.rate_limits = dict(rate_limits or {})
        # Hosted adapters may recover bounded numeric quota details from an SDK error
        # message after the SDK has discarded the original response body. Keep this
        # allowlisted and numeric so arbitrary provider prose never crosses the seam.
        allowed_details = {"limit_tokens", "used_tokens", "requested_tokens"}
        self.rate_limit_details = {
            key: value for key, value in (rate_limit_details or {}).items()
            if key in allowed_details and isinstance(value, (int, float))
            and not isinstance(value, bool) and 0 <= value <= 1_000_000_000_000
        }
        # This is a bounded vocabulary safe to persist and expose to readers. Infer only
        # the one legacy transport phrase whose callers predate the category field; all
        # other legacy admission errors are ordinary rate limiting/backpressure.
        inferred = "unreachable" if "unreachable" in message.lower() else "rate_limited"
        self.category = category if category in ADMISSION_CATEGORIES else inferred


class ProviderError(Exception):
    """A normalized, non-retryable provider failure.

    Providers must raise one of the specific subclasses below instead of exposing SDK
    exception types to pipeline code (§5.4).
    """

    category = "provider_error"


class RequestBudgetExceeded(ProviderError):
    """The request cannot fit the provider's context/request budget."""

    category = "request_budget"


class UnsupportedSchema(ProviderError):
    """The selected provider/model cannot transport the requested schema."""

    category = "unsupported_schema"


class TruncatedOutput(ProviderError):
    """The provider stopped at its output limit before a complete answer."""

    category = "truncated_output"


class PinnedModelChanged(ProviderError):
    """A pinned request was served by a different provider/model."""

    category = "model_changed"


# Descriptive aliases kept for callers that use the shorter error names.
RequestBudgetError = RequestBudgetExceeded
RequestTooLarge = RequestBudgetExceeded
SchemaNotSupported = UnsupportedSchema
UnsupportedSchemaError = UnsupportedSchema
OutputTruncated = TruncatedOutput
TruncatedOutputError = TruncatedOutput


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
        max_output_tokens: int | None = None,
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
                call_options = dict(system=req["system"], json_mode=req.get("json_mode", False),
                                    cls=Class.BATCH, pin_model=req.get("pin_model", False),
                                    model=req.get("model"), **schema_options)
                if req.get("max_output_tokens") is not None:
                    call_options["max_output_tokens"] = req["max_output_tokens"]
                completion = await self.complete(req["prompt"], **call_options)  # type: ignore[attr-defined]
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


# A rate limit is usually a per-MINUTE quota, so retrying seconds later is guaranteed to
# hit it again. Measured against Gemini's free tier, a 5s default produced a hot loop:
# 7 429s to 2 successes, no chapter progressing, quota burned on retries. Server-supplied
# Retry-After still wins when present.
RATE_LIMIT_RETRY_S = 30.0

# Providers that supply no Retry-After header often put the wait in the error body
# instead. Gemini answers a 429 with "Please retry in 55.511344849s." and a structured
# retryDelay; obeying it beats any constant, and on a 20-request-per-minute quota a wait
# that is too short simply spends another request on a second 429.
_RETRY_HINT = re.compile(r'(?:retry in|"?retryDelay"?\s*:\s*")\s*([0-9]+(?:\.[0-9]+)?)\s*s', re.I)


def _retry_hint_seconds(body: str) -> float | None:
    """Seconds the provider asked us to wait, from its error body. None if it said none."""
    match = _RETRY_HINT.search(body)
    return float(match.group(1)) if match else None


def _body_of(exc: "httpx.HTTPStatusError") -> str:
    """The error body, or "" if it cannot be read. Diagnostics must never mask a failure."""
    try:
        return exc.response.text
    except Exception:  # noqa: BLE001
        return ""


def _quota_exhausted(body: str, retry_after: str) -> bool:
    """Classify a 429 while its body is in memory; never return the body itself."""
    try:
        if float(retry_after) > 3600:
            return True
    except (TypeError, ValueError):
        pass
    lowered = body.lower()
    if any(marker in lowered for marker in ("per-day", "per day", "daily", "24 hours", "day quota")):
        return True
    hinted = _retry_hint_seconds(body)
    return hinted is not None and hinted > 3600


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
            hinted = True
            delay = _retry_hint_seconds(_body_of(exc)) or 0.0
        else:
            hinted = True
        if not delay:
            hinted = False
            # 429 gets its own, much longer floor: a server that is out of quota this
            # minute will still be out of quota five seconds from now.
            delay = RATE_LIMIT_RETRY_S if exc.response.status_code == 429 else default_retry_s
        body = _body_of(exc)
        if exc.response.status_code == 429:
            category = "quota_exhausted" if _quota_exhausted(
                body, exc.response.headers.get("retry-after", "")
            ) else "rate_limited"
        elif exc.response.status_code >= 500:
            category = "model_server_error"
        else:
            category = "unreachable"
        raise AdmissionRejected(
            category,
            retry_after_s=max(delay, 0.0),
            exact_hint=hinted,
            category=category,
        ) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError) as exc:
        raise AdmissionRejected("unreachable", retry_after_s=default_retry_s,
                                category="unreachable") from exc
