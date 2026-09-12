"""GraphWriter's surviving surface: DISPLAY_SCAN's mention spans (instructions.md §4).

Entity, alias, fact, edge and event writes moved to :mod:`pipeline.records_publish` when
the records pipeline landed, and migration 0089 dropped the tables the old writers
targeted, so the only discipline left to pin here is replace-not-append.

Needs a live Postgres — skipped cleanly via the db_conn fixture when one isn't reachable
(conftest.py).
"""

from __future__ import annotations

import pytest

from pipeline.graph import GraphWriter
from pipeline.mentions import Span

from fixtures import delete_novel, make_novel, seed_entities

pytestmark = pytest.mark.db


async def _writer(conn) -> GraphWriter:
    w = GraphWriter(conn)
    await w.ready()
    return w


async def test_replace_mention_spans_is_idempotent(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        known = await seed_entities(db_conn, novel_id, {"Li Xiaoyao": "character"})
        entity_id = known["Li Xiaoyao"]
        writer = await _writer(db_conn)
        spans = [Span(alias_id=entity_id, byte_start=0, byte_end=10, char_start=0, char_end=10)]

        async with db_conn.transaction():
            await writer.replace_mention_spans(novel_id, 1, spans)
        async with db_conn.transaction():
            await writer.replace_mention_spans(novel_id, 1, spans)

        row = await (
            await db_conn.execute(
                "SELECT count(*) FROM mention_span WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, 1),
            )
        ).fetchone()
        assert row[0] == 1  # not 2 — replace, not append
    finally:
        await delete_novel(db_conn, novel_id)


async def test_replace_mention_spans_drops_spans_that_are_gone(db_conn):
    """A re-scan that finds fewer mentions must leave fewer rows.

    Append-only is the rule for knowledge, not for this projection: a glossary correction
    can legitimately unmake a highlight, and a stale span would keep pointing the reader at
    an entity the text no longer names there.
    """
    novel_id = await make_novel(db_conn)
    try:
        known = await seed_entities(db_conn, novel_id, {"Li Xiaoyao": "character"})
        entity_id = known["Li Xiaoyao"]
        writer = await _writer(db_conn)
        async with db_conn.transaction():
            await writer.replace_mention_spans(novel_id, 1, [
                Span(alias_id=entity_id, byte_start=0, byte_end=10, char_start=0, char_end=10),
                Span(alias_id=entity_id, byte_start=20, byte_end=30, char_start=20, char_end=30),
            ])
        async with db_conn.transaction():
            await writer.replace_mention_spans(novel_id, 1, [
                Span(alias_id=entity_id, byte_start=0, byte_end=10, char_start=0, char_end=10),
            ])

        rows = await (
            await db_conn.execute(
                "SELECT char_start FROM mention_span WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, 1),
            )
        ).fetchall()
        assert [r[0] for r in rows] == [0]
    finally:
        await delete_novel(db_conn, novel_id)
