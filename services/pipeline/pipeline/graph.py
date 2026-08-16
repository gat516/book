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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pgvector import Vector
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
class CandidateRow:
    """An existing entity offered to RESOLVE's disambiguator. Read-only; the write rows
    below are what actually go to Postgres."""

    id: str
    canonical: str
    kind: str


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

    async def exact_matches(self, novel_id: str, surface: str) -> list[CandidateRow]:
        """Entities whose canonical name or one of whose aliases IS this surface.

        The cheap, certain half of retrieve-then-resolve. A hit here still goes to the
        disambiguator when it is ambiguous (two characters genuinely called "Chen"), but
        it is also what catches a candidate the ANN search missed — which is why 0003's
        comment can say approximate search is acceptable for this lookup and not for
        chunks. Exact match is the safety net under the approximation.
        """
        rows = await (
            await self.db.execute(
                """
                SELECT DISTINCT e.id, e.canonical, e.kind
                FROM entity e
                LEFT JOIN alias a ON a.entity_id = e.id
                WHERE e.novel_id = %s AND (a.surface = %s OR e.canonical = %s)
                """,
                (novel_id, surface, surface),
            )
        ).fetchall()
        return [CandidateRow(id=str(r[0]), canonical=r[1], kind=r[2]) for r in rows]

    async def similar_entities(
        self, novel_id: str, embedding: list[float], *, k: int = 5
    ) -> list[CandidateRow]:
        """Nearest entities by cosine distance over ``entity.embedding`` (pgvector).

        This is what finds "Azure Cloud Sect" when the chapter says "the Azure Sect" —
        the case exact matching cannot reach and the reason entity embeddings exist.
        Uses the HNSW index from 0004, which (unlike the ivfflat it replaced) is built
        for a table that starts empty and grows by streaming inserts — exactly this
        workload, since entities are created chapter by chapter.

        Entities with a NULL embedding are skipped rather than ranked: they are the
        1.5-era rows the placeholder binder created, and a NULL sorts unhelpfully.
        """
        rows = await (
            await self.db.execute(
                """
                SELECT e.id, e.canonical, e.kind
                FROM entity e
                WHERE e.novel_id = %s AND e.embedding IS NOT NULL
                ORDER BY e.embedding <=> %s
                LIMIT %s
                """,
                # Vector(), not a bare list: an INSERT coerces a Python list into a
                # vector column happily (which is why the write paths above pass lists),
                # but as an OPERATOR argument the same list adapts to double precision[]
                # and `vector <=> double precision[]` does not exist. The asymmetry is
                # easy to "simplify" away and the result is a hard error, not a silent
                # one — so this stays explicit.
                (novel_id, Vector(embedding), k),
            )
        ).fetchall()
        return [CandidateRow(id=str(r[0]), canonical=r[1], kind=r[2]) for r in rows]

    async def insert_entity(self, entity: EntityRow, aliases: list[AliasRow]) -> None:
        """Create one entity and its aliases immediately.

        One at a time, not batched at end-of-chapter, because a surface resolved later in
        the same chapter must be able to match an entity created earlier in it. Batching
        would make a chapter that introduces a character under two names create two
        entities — the precise drift this stage exists to prevent, reintroduced as a
        write-ordering detail.
        """
        await self.upsert_entities([entity])
        await self.upsert_aliases(aliases)

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
