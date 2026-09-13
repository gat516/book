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

async def retrieve(conn: AsyncConnection, novel_id: str, at: int, embedding: list[float] | None, *, question: str = "", max_chunks: int, max_entities: int = 8, max_records: int = 64) -> list[Source]:
    """Retrieve chunks and active-generation records under the reader chapter gate.

    Record retrieval is entity-first: the nearest visible entities seed a bounded
    participant-linked result set. If no such entities have records, the query falls
    back to recent visible rows. Every predicate is repeated here because Ask AI's
    connection role may be privileged in a deployment and RLS is only one layer of
    the spoiler boundary.
    """
    # ``embedding=None`` is the native-only mode.  It lets Ask AI answer from
    # published fact-first assertions while semantic retrieval is unavailable.
    vector = vector_literal(embedding) if embedding else None
    async with conn.cursor() as cur:
        if vector is not None:
            await cur.execute("""SELECT id, chapter_index, text FROM chunk
          WHERE novel_id=%s AND chapter_index<=%s AND embedding IS NOT NULL
          ORDER BY embedding <=> %s::vector, id LIMIT %s""", (novel_id, at, vector, max_chunks))
            chunks = [Source("chunk", row[0], row[1], row[2]) for row in await cur.fetchall()]
        else:
            chunks = []
        if vector is None:
            native = await _retrieve_native(conn, novel_id, at, max_records)
            return native
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
        # Fact-first native outputs have no embedding column and must remain usable
        # when embeddings are disabled.  Keep this query separate from the legacy
        # typed-record path above: both can coexist while old generations drain.
        native = await _retrieve_native(conn, novel_id, at, max_records)
    return chunks + records + native


async def _retrieve_native(
    conn: AsyncConnection, novel_id: str, at: int, max_records: int,
) -> list[Source]:
    """Read published fact-first assertions under the active-generation gate.

    ``source_chapter`` is the reader authorization boundary; valid-from/story-time
    qualifiers are carried as context only.  Evidence is stored as JSON by the
    persistence adapter, so hovercards and citations keep the original quote and
    offsets without requiring a second embedding lookup.
    """
    query = """WITH active AS (
      SELECT active_record_generation AS generation_id
        FROM novel WHERE id=%s
    ), native AS (
      SELECT f.id::text AS id, r.chapter_index, 'fact'::text AS kind,
             f.source_chapter, f.valid_from_chapter, f.assertion_id,
             f.subject_ref AS left_ref, NULL::text AS right_ref,
             f.attribute AS label, f.value AS value, f.source_value,
             f.polarity, f.attribution, f.condition, f.temporal,
             NULL::jsonb AS arguments, f.evidence,
             jsonb_build_object('ref', f.subject_ref, 'source_name', ep.source_name,
                                'persistent_entity_id', ep.persistent_entity_id,
                                'resolution_status', ep.resolution_status) AS left_entity,
             NULL::jsonb AS right_entity,
             (SELECT jsonb_agg(jsonb_build_object('output_kind', rr.output_kind,
                       'output_id', rr.output_id, 'target_value', rr.target_value,
                       'status', rr.status, 'error_detail', rr.error_detail))
                FROM fact_first_rendering rr
               WHERE rr.run_id=f.run_id AND rr.assertion_id=f.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=f.local_id) AS rendering
        FROM fact_first_fact f
        JOIN fact_first_run r ON r.id=f.run_id
        JOIN active a ON a.generation_id=r.generation_id
        LEFT JOIN fact_first_entity_proposal ep
          ON ep.run_id=f.run_id AND ep.proposal_id=f.subject_ref
       WHERE r.novel_id=%s AND r.status='published' AND f.source_chapter<=%s
      UNION ALL
      SELECT x.id::text, r.chapter_index, 'relation'::text,
             x.source_chapter, x.valid_from_chapter, x.assertion_id,
             x.src_ref, x.dst_ref, x.relation, NULL::text, x.source_value,
             x.polarity, x.attribution, x.condition, x.temporal,
             NULL::jsonb, x.evidence,
             jsonb_build_object('ref', x.src_ref, 'source_name', eps.source_name,
                                'persistent_entity_id', eps.persistent_entity_id,
                                'resolution_status', eps.resolution_status),
             jsonb_build_object('ref', x.dst_ref, 'source_name', epd.source_name,
                                'persistent_entity_id', epd.persistent_entity_id,
                                'resolution_status', epd.resolution_status),
             (SELECT jsonb_agg(jsonb_build_object('output_kind', rr.output_kind,
                       'output_id', rr.output_id, 'target_value', rr.target_value,
                       'status', rr.status, 'error_detail', rr.error_detail))
                FROM fact_first_rendering rr
               WHERE rr.run_id=x.run_id AND rr.assertion_id=x.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=x.local_id)
        FROM fact_first_relation x
        JOIN fact_first_run r ON r.id=x.run_id
        JOIN active a ON a.generation_id=r.generation_id
        LEFT JOIN fact_first_entity_proposal eps
          ON eps.run_id=x.run_id AND eps.proposal_id=x.src_ref
        LEFT JOIN fact_first_entity_proposal epd
          ON epd.run_id=x.run_id AND epd.proposal_id=x.dst_ref
       WHERE r.novel_id=%s AND r.status='published' AND x.source_chapter<=%s
      UNION ALL
      SELECT e.id::text, r.chapter_index, 'event'::text,
             e.source_chapter, e.valid_from_chapter, e.assertion_id,
             NULL::text, NULL::text, e.action, NULL::text, e.source_value,
             e.polarity, e.attribution, e.condition, e.temporal,
             e.arguments, e.evidence,
             (SELECT jsonb_agg(jsonb_build_object(
                       'ref', arg->>'entity_id', 'role', arg->>'role',
                       'source_name', COALESCE(ep.source_name, ref.surface, arg->>'entity_id'),
                       'persistent_entity_id', ep.persistent_entity_id,
                       'reference_id', CASE WHEN ep.persistent_entity_id IS NULL THEN ref.reference_id END,
                       'resolution_status', ep.resolution_status)
                ORDER BY a.ord)
                FROM jsonb_array_elements(e.arguments) WITH ORDINALITY a(arg, ord)
                LEFT JOIN fact_first_entity_proposal ep
                  ON ep.run_id=e.run_id AND ep.proposal_id=arg->>'entity_id'
                LEFT JOIN fact_first_reference ref
                  ON ref.run_id=e.run_id AND ref.reference_id=arg->>'entity_id'
               WHERE arg ? 'entity_id'), NULL::jsonb
             ,(SELECT jsonb_agg(jsonb_build_object('output_kind', rr.output_kind,
                       'output_id', rr.output_id, 'target_value', rr.target_value,
                       'status', rr.status, 'error_detail', rr.error_detail))
                FROM fact_first_rendering rr
               WHERE rr.run_id=e.run_id AND rr.assertion_id=e.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=e.local_id)
        FROM fact_first_event e
        JOIN fact_first_run r ON r.id=e.run_id
        JOIN active a ON a.generation_id=r.generation_id
       WHERE r.novel_id=%s AND r.status='published' AND e.source_chapter<=%s
    )
    SELECT id, chapter_index, kind, source_chapter, valid_from_chapter,
           assertion_id, left_ref, right_ref, label, value, source_value,
           polarity, attribution, condition, temporal, arguments, evidence,
           left_entity, right_entity, rendering
      FROM native
     ORDER BY source_chapter DESC, id DESC
     LIMIT %s"""
    params = (novel_id, novel_id, at, novel_id, at, novel_id, at, max_records)
    async with conn.cursor() as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    result: list[Source] = []
    for row in rows:
        (row_id, chapter, kind, source_chapter, valid_from, assertion_id,
         left_ref, right_ref, label, value, source_value, polarity, attribution,
         condition, temporal, arguments, evidence, left_entity, right_entity,
         rendering) = row
        def decode(value):
            if isinstance(value, (list, dict)) or value is None:
                return value
            return json.loads(value)
        evidence = decode(evidence) or []
        arguments = decode(arguments) or []
        left_entity = decode(left_entity)
        right_entity = decode(right_entity)
        rendering = decode(rendering) or []
        qualifiers = [
            f"polarity={polarity}" if polarity else "",
            f"attribution={attribution}" if attribution else "",
            f"condition={condition}" if condition else "",
            f"temporal={temporal}" if temporal else "",
            f"valid_from_chapter={valid_from}" if valid_from is not None else "",
        ]
        qualifiers = ", ".join(item for item in qualifiers if item)
        if kind == "fact":
            text = f"fact {label}: {value} (subject {left_ref})"
        elif kind == "relation":
            text = f"relation {left_ref} {label} {right_ref}"
        else:
            text = f"event {label}"
            if arguments:
                text += f" args={json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))}"
        if source_value:
            text += f"; source: {source_value}"
        ready = [item.get("target_value") for item in rendering
                 if isinstance(item, dict) and item.get("status") == "ready"
                 and item.get("target_value")]
        if ready:
            text += f"; English: {'; '.join(str(item) for item in ready)}"
        elif rendering and any(isinstance(item, dict) and item.get("status") == "failed"
                               for item in rendering):
            text += "; English rendering failed"
        if qualifiers:
            text += f" [{qualifiers}]"
        entities = []
        for item in (left_entity, right_entity):
            if isinstance(item, list):
                entities.extend(item)
            elif item:
                entities.append(item)
        if entities:
            names = ", ".join(
                f"{item.get('ref')}: {item.get('source_name') or 'unresolved'}"
                for item in entities
            )
            text += f" (entities: {names})"
        result.append(Source("fact_first_" + kind, str(row_id), chapter, text, evidence))
    return result

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
