"""GraphWriter (PLAN.md 1.4 Task 5, instructions.md §4). Needs a live Postgres —
skipped cleanly via the db_conn fixture when one isn't reachable (conftest.py).
"""

from __future__ import annotations

import uuid

import pytest

from pipeline.context import Chunk
from pipeline.graph import EntityRow, FactRow, GraphWriter

from fixtures import delete_novel, make_novel

pytestmark = pytest.mark.db


async def _writer(conn) -> GraphWriter:
    w = GraphWriter(conn)
    await w.ready()
    return w


async def test_replace_chunks_is_idempotent(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        writer = await _writer(db_conn)
        chunks = [
            Chunk(ordinal=0, text="chunk zero", char_start=0, char_end=10),
            Chunk(ordinal=1, text="chunk one", char_start=10, char_end=19),
        ]
        embeddings = [[0.1] * 768, [0.2] * 768]

        async with db_conn.transaction():
            await writer.replace_chunks(novel_id, 1, chunks, embeddings)
        async with db_conn.transaction():
            await writer.replace_chunks(novel_id, 1, chunks, embeddings)

        row = await (
            await db_conn.execute(
                "SELECT count(*) FROM chunk WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, 1),
            )
        ).fetchone()
        assert row[0] == 2  # not 4 — replace, not append
    finally:
        await delete_novel(db_conn, novel_id)


async def test_upsert_entities_preserves_first_seen_chapter(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        writer = await _writer(db_conn)
        entity_id = str(uuid.uuid4())
        await writer.upsert_entities(
            [EntityRow(id=entity_id, novel_id=novel_id, kind="character", canonical="Alice", first_seen_chapter=5)]
        )
        # Re-upsert same id claiming an earlier first_seen_chapter — must be ignored.
        await writer.upsert_entities(
            [EntityRow(id=entity_id, novel_id=novel_id, kind="character", canonical="Alice", first_seen_chapter=1)]
        )
        row = await (
            await db_conn.execute("SELECT first_seen_chapter FROM entity WHERE id = %s", (entity_id,))
        ).fetchone()
        assert row[0] == 5
    finally:
        await delete_novel(db_conn, novel_id)


async def test_insert_facts_sets_both_chapter_columns(db_conn):
    novel_id = await make_novel(db_conn)
    try:
        writer = await _writer(db_conn)
        entity_id = str(uuid.uuid4())
        await writer.upsert_entities(
            [EntityRow(id=entity_id, novel_id=novel_id, kind="character", canonical="Bob", first_seen_chapter=3)]
        )
        ids = await writer.insert_facts(
            [
                FactRow(
                    novel_id=novel_id,
                    entity_id=entity_id,
                    attribute="status",
                    value="alive",
                    valid_from_chapter=3,
                    source_chapter=7,
                )
            ]
        )
        row = await (
            await db_conn.execute(
                "SELECT valid_from_chapter, source_chapter, kind FROM fact WHERE id = %s", (ids[0],)
            )
        ).fetchone()
        assert row == (3, 7, "assertion")
    finally:
        await delete_novel(db_conn, novel_id)
