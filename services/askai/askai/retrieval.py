from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection


@dataclass(frozen=True)
class Source:
    kind: str
    id: int | str
    chapter: int
    text: str
    evidence: dict | None = None


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


async def retrieve(conn: AsyncConnection, novel_id: str, at: int, embedding: list[float], *, question: str = "", max_chunks: int, max_entities: int, max_facts: int, max_edges: int, max_events: int = 24) -> list[Source]:
    vector = vector_literal(embedding)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id, chapter_index, text FROM chunk WHERE novel_id = %s AND chapter_index <= %s ORDER BY embedding <=> %s::vector, id LIMIT %s", (novel_id, at, vector, max_chunks))
        chunks = [Source("chunk", row[0], row[1], row[2]) for row in await cur.fetchall()]
        # Events are already compact retrieval units, so lexical ranking avoids another
        # embedding or model call. RLS independently selects active_event_revision and
        # applies the same source-chapter authorization boundary as every other source.
        await cur.execute("""SELECT e.id::text,e.chapter_index,
          e.action || CASE WHEN count(a.*)>0 THEN ' (' || string_agg(a.role || ': ' || a.surface, ', ' ORDER BY a.ordinal) || ')' ELSE '' END ||
          CASE WHEN e.result IS NOT NULL THEN '. Result: ' || e.result ELSE '' END || '. ' || e.summary,
          jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash,'char_start',v.char_start,'char_end',v.char_end),
          ts_rank(to_tsvector('simple',e.action || ' ' || e.summary || ' ' || coalesce(e.result,'') || ' ' || coalesce(string_agg(a.surface,' '),'')),websearch_to_tsquery('simple',%s)) AS rank
          FROM chapter_event e JOIN event_evidence v ON v.id=e.evidence_id
          LEFT JOIN chapter_event_argument a ON a.event_id=e.id
          WHERE e.novel_id=%s AND e.chapter_index<=%s
          GROUP BY e.id,v.id
          ORDER BY rank DESC,e.chapter_index DESC,e.id LIMIT %s""", (question, novel_id, at, max_events))
        events = [Source("event", row[0], row[1], row[2], row[3]) for row in await cur.fetchall()]
        await cur.execute("SELECT id::text FROM entity WHERE novel_id = %s AND revision_id=reader_graph_revision() AND first_seen_chapter <= %s AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector, id LIMIT %s", (novel_id, at, vector, max_entities))
        entity_ids = [row[0] for row in await cur.fetchall()]
        if not entity_ids:
            return events + chunks
        await cur.execute("""WITH visible AS (SELECT f.* FROM fact f WHERE f.novel_id = %s AND f.entity_id = ANY(%s::uuid[]) AND f.source_chapter <= %s AND f.valid_from_chapter <= %s), current_facts AS (SELECT DISTINCT ON (entity_id, attribute) * FROM visible f WHERE f.kind <> 'retraction' AND NOT EXISTS (SELECT 1 FROM visible successor WHERE successor.supersedes = f.id) ORDER BY entity_id, attribute, valid_from_chapter DESC, source_chapter DESC, confidence DESC, id DESC) SELECT cf.id, cf.source_chapter, e.canonical || ': ' || attribute || ' = ' || coalesce(cf.value_en, cf.value), (SELECT jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash) FROM graph_evidence v WHERE v.id=cf.evidence_id) FROM current_facts cf JOIN entity e ON e.id = cf.entity_id ORDER BY source_chapter DESC, id DESC LIMIT %s""", (novel_id, entity_ids, at, at, max_facts))
        facts = [Source("fact", row[0], row[1], row[2], row[3]) for row in await cur.fetchall()]
        await cur.execute("""SELECT ed.id, ed.source_chapter, src.canonical || ' --' || ed.rel_type || '--> ' || dst.canonical, (SELECT jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash) FROM graph_evidence v WHERE v.id=ed.evidence_id) FROM edge ed JOIN entity src ON src.id = ed.src_id JOIN entity dst ON dst.id = ed.dst_id WHERE ed.novel_id = %s AND (ed.src_id = ANY(%s::uuid[]) OR ed.dst_id = ANY(%s::uuid[])) AND ed.source_chapter <= %s AND ed.valid_from_chapter <= %s AND (ed.valid_to_chapter IS NULL OR ed.valid_to_chapter > %s) ORDER BY ed.source_chapter DESC, ed.id DESC LIMIT %s""", (novel_id, entity_ids, entity_ids, at, at, at, max_edges))
        edges = [Source("edge", row[0], row[1], row[2], row[3]) for row in await cur.fetchall()]
    return events + chunks + facts + edges


def build_context(sources: list[Source], max_chars: int) -> tuple[str, list[dict[str, int | str | dict]]]:
    parts: list[str] = []
    used: list[dict[str, int | str | dict]] = []
    total = 0
    for source in sources:
        item = f"[{source.kind}:{source.id} ch:{source.chapter}]\n{source.text}\n"
        if total + len(item) > max_chars:
            continue
        parts.append(item)
        used.append({"kind": source.kind, "id": source.id, "chapter": source.chapter, **({"evidence":source.evidence} if source.evidence else {})})
        total += len(item)
    return "\n".join(parts), used
