"""Persist safe operational diagnostics, never provider messages or story text."""

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


def failure_category(exc: BaseException) -> str:
    """Classify a repair failure into a safe class for the API to show.

    Matching is on the deliberately-worded phrases this codebase raises, so it is precise
    where it can be and degrades to ``unknown`` rather than guessing. Ordering matters: the
    specific causes come first and the generic transport ones last, because a message like
    "Ollama exhausted num_predict; refusing incomplete output" contains a word that would
    otherwise read as a refused connection.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "model or inference configuration changed" in text:
        return "model_changed"
    if "serving identity changed" in text:
        return "serving_identity_changed"
    if "saved prose changed" in text or "chapter input changed" in text:
        return "input_changed"
    if "hard local model budget" in text:
        return "prompt_too_large"
    # docs/knowledge-repair.md: hitting GRAPH_OLLAMA_NUM_PREDICT is a failure, never
    # publishable partial output.
    if "num_predict" in text:
        return "output_truncated"
    if "fenced" in text:
        return "fenced"
    if "revision cannot be rebuilt" in text:
        return "revision_not_rebuildable"
    # Bounded to the phrases record_review actually raises. A bare "review" would also
    # match unrelated errors that merely mention the review column.
    if "review must" in text or "must assess" in text:
        return "review_rejected"
    if "not found" in text or "no such" in text:
        return "not_found"
    if "timeout" in text or "timed out" in text or "deadline" in text:
        return "timeout"
    if "connection refused" in text or "could not connect" in text or "unreachable" in text:
        return "model_unreachable"
    return "unknown"
