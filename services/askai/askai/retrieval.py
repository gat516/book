from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection


@dataclass(frozen=True)
class Source:
    kind: str
    id: int
    chapter: int
    text: str


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


async def retrieve(conn: AsyncConnection, novel_id: str, at: int, embedding: list[float], *, max_chunks: int, max_entities: int, max_facts: int, max_edges: int) -> list[Source]:
    vector = vector_literal(embedding)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id, chapter_index, text FROM chunk WHERE novel_id = %s AND chapter_index <= %s ORDER BY embedding <=> %s::vector, id LIMIT %s", (novel_id, at, vector, max_chunks))
        chunks = [Source("chunk", row[0], row[1], row[2]) for row in await cur.fetchall()]
        await cur.execute("SELECT id::text FROM entity WHERE novel_id = %s AND first_seen_chapter <= %s AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector, id LIMIT %s", (novel_id, at, vector, max_entities))
        entity_ids = [row[0] for row in await cur.fetchall()]
        if not entity_ids:
            return chunks
        await cur.execute("""WITH visible AS (SELECT f.* FROM fact f WHERE f.novel_id = %s AND f.entity_id = ANY(%s::uuid[]) AND f.source_chapter <= %s AND f.valid_from_chapter <= %s), current_facts AS (SELECT DISTINCT ON (entity_id, attribute) * FROM visible f WHERE f.kind <> 'retraction' AND NOT EXISTS (SELECT 1 FROM visible successor WHERE successor.supersedes = f.id) ORDER BY entity_id, attribute, valid_from_chapter DESC, source_chapter DESC, confidence DESC, id DESC) SELECT id, source_chapter, e.canonical || ': ' || attribute || ' = ' || value FROM current_facts cf JOIN entity e ON e.id = cf.entity_id ORDER BY source_chapter DESC, id DESC LIMIT %s""", (novel_id, entity_ids, at, at, max_facts))
        facts = [Source("fact", row[0], row[1], row[2]) for row in await cur.fetchall()]
        await cur.execute("""SELECT ed.id, ed.source_chapter, src.canonical || ' --' || ed.rel_type || '--> ' || dst.canonical FROM edge ed JOIN entity src ON src.id = ed.src_id JOIN entity dst ON dst.id = ed.dst_id WHERE ed.novel_id = %s AND (ed.src_id = ANY(%s::uuid[]) OR ed.dst_id = ANY(%s::uuid[])) AND ed.source_chapter <= %s AND ed.valid_from_chapter <= %s AND (ed.valid_to_chapter IS NULL OR ed.valid_to_chapter > %s) ORDER BY ed.source_chapter DESC, ed.id DESC LIMIT %s""", (novel_id, entity_ids, entity_ids, at, at, at, max_edges))
        edges = [Source("edge", row[0], row[1], row[2]) for row in await cur.fetchall()]
    return chunks + facts + edges


def build_context(sources: list[Source], max_chars: int) -> tuple[str, list[dict[str, int | str]]]:
    parts: list[str] = []
    used: list[dict[str, int | str]] = []
    total = 0
    for source in sources:
        item = f"[{source.kind}:{source.id} ch:{source.chapter}]\n{source.text}\n"
        if total + len(item) > max_chars:
            continue
        parts.append(item)
        used.append({"kind": source.kind, "id": source.id, "chapter": source.chapter})
        total += len(item)
    return "\n".join(parts), used
