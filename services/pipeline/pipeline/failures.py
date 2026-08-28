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
