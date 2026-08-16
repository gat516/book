"""Reusable DB fixtures for graph/flashback tests. Not production code — importable
helpers, not pytest fixtures themselves (those live in conftest.py / the test files)."""

from __future__ import annotations

import uuid


async def make_novel(conn, *, source_lang: str = "zh", target_lang: str = "en") -> str:
    novel_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO novel (id, title, source_lang, target_lang, ontology) "
        "VALUES (%s, %s, %s, %s, %s)",
        (novel_id, "Test Novel", source_lang, target_lang, "{}"),
    )
    return novel_id


async def delete_novel(conn, novel_id: str) -> None:
    """Manual cascade — 0001 declares FKs without ON DELETE CASCADE."""
    for table in ("fact", "edge", "event", "chunk", "alias", "glossary", "entity", "chapter", "novel"):
        if table == "alias":
            await conn.execute(
                "DELETE FROM alias WHERE entity_id IN (SELECT id FROM entity WHERE novel_id = %s)",
                (novel_id,),
            )
        elif table == "novel":
            await conn.execute("DELETE FROM novel WHERE id = %s", (novel_id,))
        else:
            await conn.execute(f"DELETE FROM {table} WHERE novel_id = %s", (novel_id,))


async def seed_flashback(conn, novel_id: str) -> dict:
    """The Phase 2 tripwire fixture (PLAN.md 1.4 Task 6): an entity revealed at
    story-time chapter 10 but whose fact isn't extracted/knowable until chapter 500 —
    a flashback. A reader at chapter 220 must not see it; the gate gates on
    source_chapter (knowledge-time), never valid_from_chapter (story-time)."""
    entity_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, %s, %s, %s)",
        (entity_id, novel_id, "character", "Flashback Character", 10),
    )
    row = await (
        await conn.execute(
            "INSERT INTO fact (novel_id, entity_id, attribute, value, "
            "valid_from_chapter, source_chapter) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (novel_id, entity_id, "secret_origin", "was the villain all along", 10, 500),
        )
    ).fetchone()
    return {"entity_id": entity_id, "fact_id": row[0]}
