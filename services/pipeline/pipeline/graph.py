"""The display-span sink for DISPLAY_SCAN (instructions.md §4).

This module used to own writes to the whole legacy knowledge graph (fact/edge/event and
the entity upserts that fed them).  The records pipeline replaced that path: identity and
rows are written by :mod:`pipeline.records_publish`, inside the generation-scoped tables,
and migration 0089 dropped the tables the rest of this module wrote to.  What remains is
the one derived surface DISPLAY_SCAN still owns.

**Spans are derived, not knowledge**: ``replace_mention_spans`` deletes a chapter's rows
and re-inserts, because re-scanning is expected to happen (a glossary correction, a re-run
after a crash) and there is no natural key to ``ON CONFLICT`` against.  Append-only (§0.2)
governs knowledge; it does not govern a recomputable projection of it.

Every method runs inside the caller's transaction — callers open one per chapter via
``async with ctx.db.transaction():`` so a mid-chapter crash leaves nothing partial.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pgvector.psycopg import register_vector_async


if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from pipeline.mentions import Span


class GraphWriter:
    """Holds the connection; one method per table. Stateless otherwise."""

    def __init__(self, db: "AsyncConnection") -> None:
        self.db = db

    async def ready(self) -> None:
        """Register the pgvector type adapter. Call once before any embedding write."""
        await register_vector_async(self.db)

    async def replace_mention_spans(
        self, novel_id: str, chapter_index: int, spans: list["Span"], renderings=()
    ) -> None:
        """Delete this chapter's existing display spans and re-insert. Same discipline
        as ``replace_chunks``: derived data, not knowledge, no natural key to
        `ON CONFLICT` against — delete-and-reinsert is what makes re-running a chapter
        idempotent (§0.7). ``span.alias_id`` carries the entity id (§4's alias-id
        convention, shared with the extraction-time scanner)."""
        async with self.db.cursor() as cur:
            await cur.execute(
                "DELETE FROM mention_span WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, chapter_index),
            )
            await cur.execute(
                "DELETE FROM term_rendering_occurrence WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, chapter_index),
            )
            if spans:
                await cur.executemany(
                    """
                    INSERT INTO mention_span (novel_id, chapter_index, entity_id, char_start, char_end)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (novel_id, chapter_index, s.alias_id or None, s.char_start, s.char_end)
                        for s in spans
                    ],
                )
            if renderings:
                await cur.executemany(
                    """INSERT INTO term_rendering_occurrence
                       (novel_id,chapter_index,char_start,char_end,source_term,display_term,method)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    [(novel_id, chapter_index, r.char_start, r.char_end, r.source_term,
                      r.display_term, r.method) for r in renderings],
                )
