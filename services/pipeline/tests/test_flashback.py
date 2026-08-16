"""The flashback tripwire (PLAN.md 1.4 Task 6, instructions.md §4.2, §12 risk #1).

A fact whose story-time (valid_from_chapter=10) is far earlier than its knowledge-time
(source_chapter=500) — the reader learns of it at ch.500 even though it "happened" at
ch.10. The as-of query MUST gate on source_chapter, never valid_from_chapter, or a
reader at ch.220 would see a chapter-500 spoiler because it "already happened" in-story.

Needs a live Postgres — skipped cleanly via db_conn (conftest.py) when unreachable.
Phase 2 builds the real RLS-only version of this test (see the plan's "Phase 2
prerequisite" note); this is the app-layer query shape, testable before reader-api
exists.
"""

from __future__ import annotations

import pytest

from fixtures import delete_novel, make_novel, seed_flashback

pytestmark = pytest.mark.db


async def _visible_at(conn, novel_id: str, entity_id: str, reader_chapter: int) -> list:
    rows = await (
        await conn.execute(
            "SELECT id FROM fact WHERE novel_id = %s AND entity_id = %s AND source_chapter <= %s",
            (novel_id, entity_id, reader_chapter),
        )
    ).fetchall()
    return rows


async def test_flashback_hidden_before_knowledge_chapter(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        fixture = await seed_flashback(db_conn, novel_id)
        # Story-time says this happened at ch.10 — a naive valid_from_chapter gate
        # would wrongly show it here. Reader is only at ch.220, well past ch.10 but
        # nowhere near the ch.500 knowledge-time.
        rows = await _visible_at(db_conn, novel_id, fixture["entity_id"], 220)
        assert rows == []
    finally:
        await delete_novel(db_conn, novel_id)


async def test_flashback_visible_after_knowledge_chapter(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        fixture = await seed_flashback(db_conn, novel_id)
        rows = await _visible_at(db_conn, novel_id, fixture["entity_id"], 500)
        assert [r[0] for r in rows] == [fixture["fact_id"]]
    finally:
        await delete_novel(db_conn, novel_id)
