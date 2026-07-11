"""ChapterEnvelope — the normalized ingestion unit (instructions.md §3.1).

Pydantic mirror of the Go struct in ``services/ingest-api/envelope.go``. The paste
adapter produced one and landed the pieces in Postgres + MinIO; the worker reconstructs
it from the ``chapter`` row (source_meta) plus the body fetched from the object store.
The site's chapter number lives in ``source_meta`` and is NEVER the gate key — the
internal ``chapter_index`` is (§3.1).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceMeta(BaseModel):
    """Provenance. ``raw_hash`` drives dedup + cache keys; ``site_chapter_no`` is metadata only."""

    source_url: str = ""
    site_chapter_no: str = ""
    fetched_at: str = ""  # RFC3339
    raw_hash: str = ""  # "sha256:..." of raw_text
    adapter: str = ""  # "scrape" | "paste"


class ChapterEnvelope(BaseModel):
    novel_id: str
    chapter_index: int  # INTERNAL canonical index, not the site's number
    raw_text: str  # extracted source-language body, no nav/ads
    source_lang: str
    source_meta: SourceMeta = Field(default_factory=SourceMeta)


class QueueMessage(BaseModel):
    """The lightweight pointer LPUSHed onto ``jobs:pending`` (mirror of the Go struct).

    Only identifiers cross the queue; the body is loaded from ``chapter.raw_uri``.
    """

    novel_id: str
    chapter_index: int
