from __future__ import annotations

from dataclasses import dataclass
import json
from psycopg import AsyncConnection

@dataclass(frozen=True)
class Source:
    kind: str
    id: int | str
    chapter: int
    text: str
    evidence: list[dict] | dict | None = None

def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"

async def retrieve(conn: AsyncConnection, novel_id: str, at: int, embedding: list[float], *, question: str = "", max_chunks: int, max_entities: int = 8, max_records: int = 64) -> list[Source]:
    """Retrieve chunks and active-generation records under the reader chapter gate.

    Record retrieval is entity-first: the nearest visible entities seed a bounded
    participant-linked result set. If no such entities have records, the query falls
    back to recent visible rows. Every predicate is repeated here because Ask AI's
    connection role may be privileged in a deployment and RLS is only one layer of
    the spoiler boundary.
    """
    vector = vector_literal(embedding)
    async with conn.cursor() as cur:
        await cur.execute("""SELECT id, chapter_index, text FROM chunk
          WHERE novel_id=%s AND chapter_index<=%s AND embedding IS NOT NULL
          ORDER BY embedding <=> %s::vector, id LIMIT %s""", (novel_id, at, vector, max_chunks))
        chunks = [Source("chunk", row[0], row[1], row[2]) for row in await cur.fetchall()]
        # Active generation and published run are explicit here as well as in RLS.
        # This keeps Ask AI fail-closed when called with a privileged local role.
        await cur.execute("""SELECT e.id::text
          FROM entity e JOIN novel n ON n.id=e.novel_id
         WHERE e.novel_id=%s AND e.record_generation_id=n.active_record_generation
           AND e.first_seen_chapter<=%s AND e.embedding IS NOT NULL
           AND EXISTS (
             SELECT 1 FROM record_participant p
             JOIN record_row er ON er.id=p.row_id
             JOIN record_run eu ON eu.id=er.run_id
            WHERE p.entity_id=e.id AND er.novel_id=e.novel_id
              AND er.generation_id=e.record_generation_id
              AND eu.status='published' AND er.source_chapter<=%s
           )
         ORDER BY (
           EXISTS (SELECT 1 FROM alias a WHERE a.entity_id=e.id
                    AND a.first_seen_chapter<=%s
                    AND position(lower(a.surface) in lower(%s)) > 0)
           OR EXISTS (SELECT 1 FROM glossary g WHERE g.novel_id=e.novel_id
                    AND g.entity_id=e.id AND g.locked_at_chapter<=%s
                    AND position(lower(g.target_term) in lower(%s)) > 0)
         ) DESC, e.embedding <=> %s::vector, e.id LIMIT %s""",
            (novel_id, at, at, at, question, at, question, vector, max_entities))
        entity_ids = [row[0] for row in await cur.fetchall()]

        # Use a participant-linked result whenever the entity snapshot has a match.
        # A chapter-recency fallback keeps broad questions useful for books whose
        # entity embeddings have not been populated yet.
        entity_filter = "AND EXISTS (SELECT 1 FROM record_participant ep WHERE ep.row_id=r.id AND ep.entity_id = ANY(%s::uuid[]))" if entity_ids else ""
        await cur.execute(f"""WITH active AS (
            SELECT active_record_generation AS generation_id FROM novel WHERE id=%s
          ), visible_rows AS (
            SELECT r.id, r.run_id, r.source_chapter, r.record_type,
                   r.valid_from_chapter, r.temporal_qualifier, r.original_index
              FROM record_row r JOIN record_run run ON run.id=r.run_id
              JOIN active a ON a.generation_id=r.generation_id
             WHERE r.novel_id=%s AND run.novel_id=%s AND run.generation_id=a.generation_id
               AND run.status='published' AND r.source_chapter<=%s
               {entity_filter}
          ), vals AS (
            SELECT v.row_id,
              jsonb_agg(jsonb_build_object(
                'field', v.field_name,
                'source', v.source_value,
                'rendered', CASE WHEN rr.status='ready' THEN rr.target_value ELSE NULL END,
                'render_status', coalesce(rr.status,'pending')
              ) ORDER BY v.field_name) AS fields
              FROM record_value v LEFT JOIN record_rendering rr
                ON rr.row_id=v.row_id AND rr.field_name=v.field_name
             GROUP BY v.row_id
          ), participants AS (
            SELECT p.row_id,
              jsonb_agg(jsonb_build_object(
                'field', p.field_name, 'surface', p.surface,
                'entity_id', p.entity_id, 'reference_id', p.reference_id
              ) ORDER BY p.ordinal) AS names
              FROM record_participant p GROUP BY p.row_id
          ), proof AS (
            SELECT ev.row_id, jsonb_agg(jsonb_build_object(
                'passage_id', pa.passage_id, 'run_id', pa.run_id,
                'chapter', pa.chapter_index, 'quote', ev.quote,
                'text', pa.text, 'char_start', pa.char_start,
                'char_end', pa.char_end, 'ordinal', pa.ordinal
              ) ORDER BY pa.ordinal) AS evidence
              FROM record_evidence ev
              JOIN record_passage pa ON pa.run_id=ev.run_id AND pa.passage_id=ev.passage_id
             GROUP BY ev.row_id
          )
          SELECT vr.id::text, vr.source_chapter, vr.record_type, vr.original_index,
                 vr.valid_from_chapter, vr.temporal_qualifier,
                 coalesce(vals.fields,'[]'::jsonb), coalesce(participants.names,'[]'::jsonb),
                 coalesce(proof.evidence,'[]'::jsonb)
            FROM visible_rows vr
            LEFT JOIN vals ON vals.row_id=vr.id
            LEFT JOIN participants ON participants.row_id=vr.id
            LEFT JOIN proof ON proof.row_id=vr.id
           ORDER BY vr.source_chapter DESC, vr.original_index DESC
           LIMIT %s""", [novel_id, novel_id, novel_id, at, *([entity_ids] if entity_ids else []), max_records])
        record_rows = await cur.fetchall()
        # If entity-linked retrieval has no rows, run the same query without the
        # participant filter. This is deliberately a second bounded query, rather than
        # mixing unrelated rows into a partial entity result.
        if entity_ids and not record_rows:
            await cur.execute("""WITH active AS (
                SELECT active_record_generation AS generation_id FROM novel WHERE id=%s
              ), visible_rows AS (
                SELECT r.id, r.run_id, r.source_chapter, r.record_type,
                       r.valid_from_chapter, r.temporal_qualifier, r.original_index
                  FROM record_row r JOIN record_run run ON run.id=r.run_id
                  JOIN active a ON a.generation_id=r.generation_id
                 WHERE r.novel_id=%s AND run.novel_id=%s AND run.generation_id=a.generation_id
                   AND run.status='published' AND r.source_chapter<=%s
              ), vals AS (
                SELECT v.row_id, jsonb_agg(jsonb_build_object(
                  'field',v.field_name,'source',v.source_value,
                  'rendered',CASE WHEN rr.status='ready' THEN rr.target_value ELSE NULL END,
                  'render_status',coalesce(rr.status,'pending')) ORDER BY v.field_name) fields
                  FROM record_value v LEFT JOIN record_rendering rr ON rr.row_id=v.row_id AND rr.field_name=v.field_name GROUP BY v.row_id
              ), participants AS (
                SELECT p.row_id, jsonb_agg(jsonb_build_object('field',p.field_name,'surface',p.surface,'entity_id',p.entity_id,'reference_id',p.reference_id) ORDER BY p.ordinal) names
                  FROM record_participant p GROUP BY p.row_id
              ), proof AS (
                SELECT ev.row_id, jsonb_agg(jsonb_build_object('passage_id',pa.passage_id,'run_id',pa.run_id,'chapter',pa.chapter_index,'quote',ev.quote,'text',pa.text,'char_start',pa.char_start,'char_end',pa.char_end,'ordinal',pa.ordinal) ORDER BY pa.ordinal) evidence
                  FROM record_evidence ev JOIN record_passage pa ON pa.run_id=ev.run_id AND pa.passage_id=ev.passage_id GROUP BY ev.row_id
              )
              SELECT vr.id::text,vr.source_chapter,vr.record_type,vr.original_index,vr.valid_from_chapter,vr.temporal_qualifier,
                     coalesce(vals.fields,'[]'::jsonb),coalesce(participants.names,'[]'::jsonb),coalesce(proof.evidence,'[]'::jsonb)
                FROM visible_rows vr LEFT JOIN vals ON vals.row_id=vr.id LEFT JOIN participants ON participants.row_id=vr.id LEFT JOIN proof ON proof.row_id=vr.id
               ORDER BY vr.source_chapter DESC,vr.original_index DESC LIMIT %s""",
                (novel_id, novel_id, novel_id, at, max_records))
            record_rows = await cur.fetchall()

        records: list[Source] = []
        for row in record_rows:
            _, chapter, record_type, original_index, valid_from, temporal, fields, participants_json, evidence = row
            fields = fields if isinstance(fields, list) else json.loads(fields)
            participants_json = participants_json if isinstance(participants_json, list) else json.loads(participants_json)
            evidence = evidence if isinstance(evidence, list) else json.loads(evidence)
            field_text = "; ".join(
                f"{item.get('field')}: {item.get('source')}" + (f" (English: {item['rendered']})" if item.get('rendered') else "")
                for item in fields if isinstance(item, dict)
            )
            participant_text = ", ".join(str(item.get('surface') or '') for item in participants_json if isinstance(item, dict))
            qualifier = f" [{temporal}]" if temporal else ""
            text = f"{record_type} (record index {original_index}){qualifier}: {field_text}"
            if participant_text:
                text += f" (participants: {participant_text})"
            records.append(Source("record", str(row[0]), chapter, text, evidence))
    return chunks + records

def build_context(sources: list[Source], max_chars: int) -> tuple[str, list[dict]]:
    parts: list[str] = []
    used: list[dict] = []
    total = 0
    for source in sources:
        evidence = ""
        if source.evidence:
            evidence = "\nEvidence:\n" + "\n".join(
                str(item.get("quote") or item.get("text") or "")
                for item in (source.evidence if isinstance(source.evidence, list) else [source.evidence])
                if isinstance(item, dict)
            )
        item = f"[{source.kind}:{source.id} ch:{source.chapter}]\n{source.text}{evidence}\n"
        if total + len(item) > max_chars:
            continue
        parts.append(item)
        used.append({"kind": source.kind, "id": source.id, "chapter": source.chapter, **({"evidence": source.evidence} if source.evidence else {})})
        total += len(item)
    return "\n".join(parts), used
