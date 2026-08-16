"""The graph-write sink (instructions.md §4, §5 step "graph-write"; PLAN.md 1.4).

``GraphWriter`` is the only thing in the pipeline that writes to the six knowledge
tables. Every LLM stage (1.5-1.7) produces plain data; only this module knows the SQL.

Two different write disciplines live here side by side, and mixing them up is §12's
risk #1:

- **Knowledge is append-only** (fact/edge/event, entity/alias upserts): rows are
  inserted, never updated. ``source_chapter`` (knowledge-time, the RLS gate key) and
  ``valid_from_chapter``/``first_seen_chapter`` (story-time, display only) are always
  both set, and never confused for each other.
- **Chunks are derived, not knowledge**: ``replace_chunks`` deletes a chapter's rows and
  re-inserts, because re-chunking is expected to happen (budget tuning, a re-run after
  a crash) and there is no natural key to `ON CONFLICT` against without adding one.

Every method runs inside the caller's transaction — callers open one per chapter via
``async with ctx.db.transaction():`` so a mid-chapter crash leaves nothing partial.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pgvector.psycopg import register_vector_async

from pipeline.context import Chunk

if TYPE_CHECKING:
    from psycopg import AsyncConnection


@dataclass(frozen=True)
class EntityRow:
    id: str  # caller-assigned: an existing id (resolve found a match) or a fresh uuid4
    novel_id: str
    kind: str
    canonical: str
    first_seen_chapter: int
    embedding: list[float] | None = None


@dataclass(frozen=True)
class AliasRow:
    entity_id: str
    surface: str
    lang: str
    first_seen_chapter: int


@dataclass(frozen=True)
class FactRow:
    novel_id: str
    entity_id: str
    attribute: str
    value: str
    valid_from_chapter: int
    source_chapter: int
    confidence: float = 1.0
    kind: str = "assertion"  # assertion|retraction|correction (0005)
    supersedes: int | None = None


@dataclass(frozen=True)
class EdgeRow:
    novel_id: str
    src_id: str
    dst_id: str
    rel_type: str
    valid_from_chapter: int
    source_chapter: int
    valid_to_chapter: int | None = None


@dataclass(frozen=True)
class EventRow:
    novel_id: str
    chapter_index: int
    summary: str
    entity_ids: list[str]


class GraphWriter:
    """Holds the connection; one method per table. Stateless otherwise."""

    def __init__(self, db: "AsyncConnection") -> None:
        self.db = db

    async def ready(self) -> None:
        """Register the pgvector type adapter. Call once before any embedding write."""
        await register_vector_async(self.db)

    async def upsert_entities(self, rows: list[EntityRow]) -> None:
        """``ON CONFLICT (id) DO NOTHING``: the caller decides new-vs-existing by which
        id it passes. A conflict means "this entity already exists" — leaving its
        original ``first_seen_chapter`` untouched is exactly the point (§4: an alias/
        entity's first-seen chapter must never move backwards after the fact, that is
        a spoiler leak in the other direction)."""
        if not rows:
            return
        async with self.db.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                [
                    (r.id, r.novel_id, r.kind, r.canonical, r.first_seen_chapter, r.embedding)
                    for r in rows
                ],
            )

    async def upsert_aliases(self, rows: list[AliasRow]) -> None:
        """``ON CONFLICT (entity_id, surface, lang) DO NOTHING`` — the same
        first-seen-preservation rule as entities, keyed on alias's own PK."""
        if not rows:
            return
        async with self.db.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO alias (entity_id, surface, lang, first_seen_chapter)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (entity_id, surface, lang) DO NOTHING
                """,
                [(r.entity_id, r.surface, r.lang, r.first_seen_chapter) for r in rows],
            )

    async def insert_facts(self, rows: list[FactRow]) -> list[int]:
        """Plain inserts — append-only. A correction is a NEW row with
        ``kind='correction'`` and ``supersedes`` pointing at the fact it replaces, never
        an UPDATE of the original (0005)."""
        if not rows:
            return []
        ids: list[int] = []
        async with self.db.cursor() as cur:
            for r in rows:
                await cur.execute(
                    """
                    INSERT INTO fact
                        (novel_id, entity_id, attribute, value, valid_from_chapter,
                         source_chapter, confidence, kind, supersedes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        r.novel_id,
                        r.entity_id,
                        r.attribute,
                        r.value,
                        r.valid_from_chapter,
                        r.source_chapter,
                        r.confidence,
                        r.kind,
                        r.supersedes,
                    ),
                )
                row = await cur.fetchone()
                ids.append(row[0])
        return ids

    async def insert_edges(self, rows: list[EdgeRow]) -> None:
        """Plain inserts — append-only, same discipline as facts."""
        if not rows:
            return
        async with self.db.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO edge
                    (novel_id, src_id, dst_id, rel_type, valid_from_chapter,
                     valid_to_chapter, source_chapter)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        r.novel_id,
                        r.src_id,
                        r.dst_id,
                        r.rel_type,
                        r.valid_from_chapter,
                        r.valid_to_chapter,
                        r.source_chapter,
                    )
                    for r in rows
                ],
            )

    async def insert_events(self, rows: list[EventRow]) -> None:
        """Plain inserts — append-only. ``chapter_index`` is both knowledge-time and
        story-time for events (§4); there is no separate source_chapter column."""
        if not rows:
            return
        async with self.db.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO event (novel_id, chapter_index, summary, entity_ids)
                VALUES (%s, %s, %s, %s)
                """,
                [(r.novel_id, r.chapter_index, r.summary, r.entity_ids) for r in rows],
            )

    async def bind_surfaces(
        self, novel_id: str, chapter_index: int, surfaces: list[tuple[str, str]], *, lang: str
    ) -> dict[str, str]:
        """PROVISIONAL surface -> entity_id binding. Replaced by RESOLVE in 1.6.

        The state extractor names entities by the string the chapter used; facts need a
        real ``entity.id``. Deciding which existing entity a surface refers to is exactly
        the retrieve-then-resolve problem §5 warns silently fails at scale — vector
        candidates plus an LLM that confirms rather than free-generates. None of that
        exists yet, so this does the honest minimum: **exact match on an existing alias
        or canonical name within the novel, otherwise a new entity.**

        That is a deliberately weak resolver and it drifts in the documented way — two
        spellings of one character become two entities. It is not a design, it is a
        placeholder that lets 1.5's writes be real; when 1.6 lands, ``state.resolutions``
        supplies the mapping and graph-write stops calling this.

        No chapter gate here on purpose: ingestion is not a read path. The spoiler gate
        (§0.3) applies to what a *reader* may see, and is enforced on the way out; an
        ingest worker resolving chapter 500 legitimately sees every entity in the novel.

        Runs inside the caller's transaction, like every other method here.
        """
        bound: dict[str, str] = {}
        new_entities: list[EntityRow] = []
        new_aliases: list[AliasRow] = []

        async with self.db.cursor() as cur:
            for surface, kind in surfaces:
                if surface in bound:
                    continue
                await cur.execute(
                    """
                    SELECT e.id FROM entity e
                    LEFT JOIN alias a ON a.entity_id = e.id
                    WHERE e.novel_id = %s AND (a.surface = %s OR e.canonical = %s)
                    LIMIT 1
                    """,
                    (novel_id, surface, surface),
                )
                row = await cur.fetchone()
                if row is not None:
                    bound[surface] = str(row[0])
                    continue
                entity_id = str(uuid.uuid4())
                bound[surface] = entity_id
                new_entities.append(
                    EntityRow(
                        id=entity_id,
                        novel_id=novel_id,
                        kind=kind,
                        canonical=surface,
                        first_seen_chapter=chapter_index,
                        # embedding stays NULL: it is resolve's input (1.6), and a wrong
                        # vector here would poison the very lookup that replaces this.
                        embedding=None,
                    )
                )
                new_aliases.append(
                    AliasRow(
                        entity_id=entity_id,
                        surface=surface,
                        lang=lang,
                        first_seen_chapter=chapter_index,
                    )
                )

        await self.upsert_entities(new_entities)
        await self.upsert_aliases(new_aliases)
        return bound

    async def replace_chunks(
        self, novel_id: str, chapter_index: int, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> None:
        """Delete this chapter's existing chunks and re-insert. Chunks are derived data
        (not knowledge, §0.2's append-only rule doesn't apply), and there is no unique
        key to `ON CONFLICT` against — delete-and-reinsert inside the caller's
        transaction is what makes re-running a chapter idempotent (§0.7)."""
        assert len(chunks) == len(embeddings), "one embedding per chunk"
        async with self.db.cursor() as cur:
            await cur.execute(
                "DELETE FROM chunk WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, chapter_index),
            )
            if chunks:
                await cur.executemany(
                    """
                    INSERT INTO chunk (novel_id, chapter_index, text, embedding)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (novel_id, chapter_index, c.text, emb)
                        for c, emb in zip(chunks, embeddings)
                    ],
                )
