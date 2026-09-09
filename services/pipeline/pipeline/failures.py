"""Persist safe operational diagnostics, never provider messages or story text."""

import re

import httpx
from pydantic import ValidationError

from pipeline.batch import BatchRequestFailed


def error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(exc, httpx.TransportError):
        return "provider_connection"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider_http_{exc.response.status_code}"
    if isinstance(exc, BatchRequestFailed):
        return "provider_batch_failed"
    if isinstance(exc, (ValidationError, ValueError)):
        return "invalid_stage_output"
    return "stage_failed"


async def record_failure(db, novel_id: str, chapter: int, stage: str, exc: Exception) -> None:
    # Exception strings can contain source text, URLs with API keys, or SQL parameters.
    # Keep the type + category durably; detailed traces remain in operator logs.
    await db.execute(
        "INSERT INTO chapter_failure (novel_id, chapter_index, stage, error_type, error_code) "
        "VALUES (%s, %s, %s, %s, %s)",
        (novel_id, chapter, stage, type(exc).__name__, error_code(exc)),
    )


# --- Knowledge-repair failures -------------------------------------------------------
#
# The same principle as error_code above, applied to the repair lifecycle: a chapter
# rebuild (graph_rebuild.resume / event_rebuild.resume) and a repair action
# (repair.drain_requests) both fail, and both must tell a reader what happened without
# handing them the exception text. That text is freeform and can carry source prose or a
# connection string, so the safe class is derived here, where the exception object is, and
# only the class is stored (migration 0046).
#
# Separate from error_code because the vocabularies answer different questions: error_code
# classifies a pipeline stage for the chapter_failure ledger, this classifies a repair
# failure for the reader-facing repair panel. Merging them would force one set of names to
# serve two audiences.
#
# reader-api renders these classes and no longer derives them, so every value returned here
# must have a sentence in repairFailureDetail in services/reader-api/repair.go —
# tests/test_repair.py enforces that across the language boundary.

# Written directly rather than derived from an exception:
#   'cancelled' -- ingest-api's CancelRepairRequest, when a request is withdrawn.
#   'abandoned' -- repair._reclaim_abandoned, when a worker died mid-action.
CANCELLED = "cancelled"
ABANDONED = "abandoned"


# A provider's 429 response is inspected only while the exception is in memory.  The
# response body is deliberately never returned or persisted: it can contain provider
# request metadata, account details, or a key-bearing URL.  One hour is long enough to
# separate an ordinary per-minute limiter from a daily/provider-wide quota without
# treating a short server-directed pause as permanent.
_QUOTA_LONG_DELAY_SECONDS = 3600.0
_RETRY_DELAY = re.compile(
    r"(?:retry\s+in|\"?retryDelay\"?\s*[:=])\s*\"?"
    r"([0-9]+(?:\.[0-9]+)?)\s*s\"?",
    re.IGNORECASE,
)
_DAILY_QUOTA = re.compile(
    r"(?:\bper[- ]day\b|\bdaily\b|\b24\s*hours?\b|\bday\s+quota\b|"
    r"\bquota\b.{0,80}\b(?:tomorrow|next\s+day|day)\b)",
    re.IGNORECASE | re.DOTALL,
)


def _is_model_request(response: httpx.Response) -> bool:
    """Return whether a 404 came from a provider model endpoint.

    A repair action can also encounter a 404 for a missing book/revision, which remains
    ``not_found``.  Provider completion/model routes are stable enough to identify from
    their URL path without looking at the provider's freeform response body.
    """
    path = response.request.url.path.lower()
    return (
        "/models/" in path
        or path.endswith("/models")
        or path.endswith("/chat/completions")
        or path.endswith("/completions")
        or path.endswith("/messages")
        or path.endswith("/generate")
    )


def _is_quota_exhausted(response: httpx.Response) -> bool:
    """Classify only provider-controlled quota indicators from a 429 response."""
    retry_after = response.headers.get("retry-after", "")
    try:
        if float(retry_after) > _QUOTA_LONG_DELAY_SECONDS:
            return True
    except (TypeError, ValueError):
        pass

    # This read is intentionally local to classification.  Do not include it in the
    # category, logs, or database values; §0/0046 allow only the safe class to persist.
    try:
        body = response.text
    except Exception:  # noqa: BLE001
        body = ""
    if _DAILY_QUOTA.search(body):
        return True
    hinted = _RETRY_DELAY.search(body)
    return bool(hinted and float(hinted.group(1)) > _QUOTA_LONG_DELAY_SECONDS)


def failure_category(exc: BaseException) -> str:
    """Classify a repair failure into a safe class for the API to show.

    Matching is on the deliberately-worded phrases this codebase raises, so it is precise
    where it can be and degrades to ``unknown`` rather than guessing. Ordering matters: the
    specific causes come first and the generic transport ones last, because a message like
    "Ollama exhausted num_predict; refusing incomplete output" contains a word that would
    otherwise read as a refused connection.
    """
    # AdmissionRejected carries a bounded category from the provider seam. The reader
    # vocabulary calls transport admission ``model_unreachable``; revision wait state
    # keeps the safer, more specific ``unreachable`` spelling separately.
    admission_category = getattr(exc, "category", None)
    if admission_category == "unreachable":
        return "model_unreachable"
    if admission_category in {
        "rate_limited", "quota_exhausted", "embed_unavailable", "model_server_error",
    }:
        return admission_category

    # Provider status codes are checked before exception text.  Provider wording is not
    # ours to control, and the response body must never become a durable diagnostic.
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "credential_rejected"
        if status == 404 and _is_model_request(exc.response):
            return "model_not_available"
        if status == 429:
            return "quota_exhausted" if _is_quota_exhausted(exc.response) else "rate_limited"
        if status >= 500:
            return "model_server_error"

    text = f"{type(exc).__name__}: {exc}".lower()
    # A long-lived worker can encounter a revision created after its process loaded an
    # older prompt contract. This is actionable process/config drift, not an unknown
    # extraction failure; restarting the worker lets the current contract resume it.
    if "prompt changed; create a new" in text:
        return "model_changed"
    if "model or inference configuration changed" in text:
        return "model_changed"
    if "serving identity changed" in text:
        return "serving_identity_changed"
    if "saved prose changed" in text or "chapter input changed" in text:
        return "input_changed"
    if "graph context exceeds hard model budget" in text:
        return "prompt_too_large"
    # docs/knowledge-repair.md: hitting GRAPH_OLLAMA_NUM_PREDICT is a failure, never
    # publishable partial output.
    if "num_predict" in text:
        return "output_truncated"
    if "fenced" in text:
        return "fenced"
    if "revision cannot be rebuilt" in text:
        return "revision_not_rebuildable"
    if "no configured api key" in text or "provider credential is missing" in text:
        return "credential_missing"
    # local_model refuses to substitute or download, so a revision pinned to a model that
    # is not on this Ollama endpoint can never advance. Common when the endpoint is a
    # tunnel to another machine whose model set differs from the one prepare saw.
    if "model is not installed" in text:
        return "model_not_installed"
    # Bounded to the phrases record_review actually raises. A bare "review" would also
    # match unrelated errors that merely mention the review column.
    if "review must" in text or "must assess" in text:
        return "review_rejected"
    if "not found" in text or "no such" in text:
        return "not_found"
    # A 5xx is not "unreachable": the endpoint answered, and it answered with its own
    # failure. Observed live as Ollama aborting a model load that had not finished within
    # its server-side OLLAMA_LOAD_TIMEOUT (5m by default) and returning 500 to
    # discover_num_ctx's load probe. Worth its own class because the fix is on the model
    # host and NOT in this repo's timeouts: GRAPH_OLLAMA_FIRST_TOKEN_SECONDS can be raised
    # to any value and will never widen a deadline the server enforces itself. Matched on
    # the exception object rather than its text; httpx's message carries only the status
    # and URL, so a phrase match would be guessing at wording it does not control.
    # provider_error wraps transport exceptions as "provider unreachable: <type>".
    # Check that explicit wrapper before the generic timeout words: a ReadTimeout while
    # opening the Ollama response means the connection disappeared, not that an admitted
    # chapter generation exhausted its first-token/idle/total budget.
    if "provider unreachable" in text:
        return "model_unreachable"
    if "timeout" in text or "timed out" in text or "deadline" in text:
        return "timeout"
    # httpx raises ConnectError("All connection attempts failed"), which matched none of
    # the phrases below and classified as unknown -- observed live when the SSH forward to
    # the GPU host dropped. The type name is part of `text`, so match it directly.
    if ("connecterror" in text or "connection refused" in text
            or "connection attempts failed" in text
            or "could not connect" in text or "unreachable" in text):
        return "model_unreachable"
    return "unknown"


async def record_blocked(db, table: str, rid: str, exc: BaseException) -> str:
    """Record why a whole revision cannot proceed, on the revision itself.

    resume() checks the model pin and opens its connections BEFORE the per-chapter try
    block, so a failure there never reaches the per-chapter recorder: the run dies and the
    panel keeps showing "0 of N done" with an empty failure ledger. This is what makes
    "unreachable endpoint" or "model not installed" visible instead of silent.

    Not attributed to a chapter on purpose. The cause is not any chapter's fault, and
    spending one of a chapter's three retries on an infrastructure problem would let a
    flapping connection permanently strand it.
    """
    category = failure_category(exc)
    # table is a literal supplied by this package, never caller input.
    await db.execute(
        f"UPDATE {table} SET blocked_category=%s, blocked_at=now() WHERE id=%s", (category, rid))
    return category


async def clear_blocked(db, table: str, rid: str) -> None:
    """Clear the blocked marker once a run gets past its preamble.

    Cleared as soon as the run can start rather than when a chapter finishes: a chapter
    takes minutes, and leaving a stale "unreachable" banner up that long after the
    endpoint came back is its own kind of lie.
    """
    await db.execute(
        f"UPDATE {table} SET blocked_category=NULL, blocked_at=NULL"
        " WHERE id=%s AND blocked_category IS NOT NULL", (rid,))
