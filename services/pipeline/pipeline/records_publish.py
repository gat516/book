"""Transactional publication for the records pipeline.

Discovery and who's-who are deliberately pure in :mod:`pipeline.records`; this module
is the only place that turns their result into the generation-scoped SQL rows.  The
novel row advisory lock fences a generation reset against a stale worker publishing
after a rebuild was activated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
import asyncio
from collections.abc import Sequence
from typing import Any

from pgvector.psycopg import register_vector_async
from psycopg.types.json import Jsonb

from pipeline.context import PipelineState, StageContext
from pipeline.passages import source_passages
from pipeline.records import deterministic_entity_id
from pipeline.records_generation import verify_generation
from pipeline.mentions import MentionScanRequest, Alias, scan_mentions, whole_name

log = logging.getLogger(__name__)

PARSER_VERSION = "records-parser-v1"


def _uuid(value: str) -> str:
    return str(uuid.UUID(value))


def _run_id(generation_id: str, chapter: int) -> str:
    return str(uuid.uuid5(uuid.UUID(generation_id), f"run:{chapter}"))


def _row_id(run_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), f"row:{index}"))


def _ref_id(run_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), f"reference:{index}"))


def _display_binding_id(alias_id: str | None, display_text: str, span: Any,
                        renderings: list[Any], resolutions: dict[str, str],
                        entity_ids: dict[str, str]) -> str | None:
    """Bind a display span only through the chapter's authoritative resolution.

    DISPLAY_SCAN supplies coordinates and (for translated text) a source/display term
    alignment. Neither spelling nor a UUID appearing elsewhere in the chapter is enough:
    glossary-only mentions and cross-bound entities remain unbound (§0.3).
    """
    bound = str(alias_id) if alias_id else ""
    if not bound:
        return None
    display_term = display_text[span.char_start:span.char_end]
    proposal_ids = {
        resolutions.get(rendering.source_term)
        for rendering in renderings
        if rendering.char_start == span.char_start and rendering.char_end == span.char_end
        and rendering.display_term == display_term
    }
    if not proposal_ids:
        # Same-language display spans have no alignment ledger; SCAN's source surface is
        # the only candidate, but it still must be present in this chapter's resolution.
        proposal_ids = {resolutions.get(display_term)}
    proposal_ids.discard(None)
    if len(proposal_ids) != 1:
        return None
    proposal_id = next(iter(proposal_ids))
    durable = entity_ids.get(proposal_id)
    return durable if durable == bound else None


async def _embedding_rows(ctx: StageContext, chunks: Sequence[Any]) -> list[list[float] | None]:
    """Best-effort chunk embeddings; never make publication depend on retrieval.

    Translation, records, and display-scan are durable chapter work. Chunk vectors are
    only a derived semantic-retrieval index, so an unavailable provider or a model width
    mismatch leaves the chunk row present with ``embedding IS NULL``. This also avoids
    poisoning a pgvector column with a vector from a changed model (§0 append-only).
    """
    if not chunks:
        return []
    try:
        vectors = await ctx.embed_provider.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks")
        expected = ctx.cfg.embed_dim
        if any(not isinstance(vector, (list, tuple)) or len(vector) != expected for vector in vectors):
            raise ValueError(f"embedding provider returned a vector with unexpected width (expected {expected})")
        return [list(vector) for vector in vectors]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — retrieval-only degradation
        log.warning("chunk embeddings unavailable; publishing chunks without vectors: %s", type(exc).__name__)
        return [None] * len(chunks)


async def _executemany(db, sql: str, rows) -> None:
    """psycopg's AsyncConnection has execute() but no executemany(); only a cursor does."""
    async with db.cursor() as cur:
        await cur.executemany(sql, rows)


def _warning_count(result: dict) -> int:
    """Count every persisted drop plus non-record resolution diagnostics once."""
    rejected_records = sum(not record.get("usable") for record in result.get("records", []))
    return (len(result.get("problems", [])) + rejected_records
            + len((result.get("resolution") or {}).get("problems", [])))


async def publish_records(ctx: StageContext, state: PipelineState) -> None:
    """Publish one complete chapter run and derived target-language chunks atomically."""
    result = state.records
    if not result:
        raise RuntimeError("records stage produced no result")
    if "fact_first" in result:
        await publish_fact_first(ctx, state, result["fact_first"])
        return
    source_hash = state.envelope.source_meta.raw_hash
    passages = source_passages(state.envelope.raw_text)
    # Embeddings are derived chapter data. Compute them before opening the publication
    # transaction, but degrade to NULL vectors if semantic retrieval is unavailable.
    target_chunks = state.chunks
    embeddings = await _embedding_rows(ctx, target_chunks)
    await register_vector_async(ctx.db)

    async with ctx.db.transaction():
        config = await verify_generation(ctx, state)
        generation_id = state.record_generation_id
        assert generation_id is not None
        chapter = state.envelope.chapter_index
        run_id = _run_id(generation_id, chapter)
        existing = await (await ctx.db.execute(
            "SELECT id::text,status,source_hash FROM record_run WHERE novel_id=%s AND generation_id=%s AND chapter_index=%s FOR UPDATE",
            (ctx.novel.id, generation_id, chapter))).fetchone()
        if existing and existing[2] != source_hash:
            raise RuntimeError("record run input changed inside an immutable generation")
        if existing and existing[1] == "published":
            return
        await ctx.db.execute(
            """INSERT INTO record_run (id,novel_id,generation_id,chapter_index,source_hash,display_hash,request_identity,extraction_model,served_model,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'processing')
               ON CONFLICT (novel_id,generation_id,chapter_index) DO UPDATE SET status='processing',source_hash=EXCLUDED.source_hash,
                 display_hash=EXCLUDED.display_hash,request_identity=EXCLUDED.request_identity,extraction_model=EXCLUDED.extraction_model,served_model=EXCLUDED.served_model""",
            (run_id, ctx.novel.id, generation_id, chapter, source_hash,
             hashlib.sha256((state.translation or state.envelope.raw_text).encode()).hexdigest(),
             str(result.get("request_identity") or hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()),
             config["requested_model"], result.get("served_model")))

        await ctx.db.execute("DELETE FROM record_passage WHERE run_id=%s", (run_id,))
        await _executemany(ctx.db,
            """INSERT INTO record_passage (novel_id,generation_id,run_id,chapter_index,passage_id,text,char_start,char_end,ordinal,source_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(ctx.novel.id, generation_id, run_id, chapter, p["id"], p["text"],
              p["char_start"], p["char_end"], i, source_hash) for i, p in enumerate(passages)])

        resolution = result.get("resolution") or {}
        proposal_map = resolution.get("proposal_map") or {}
        entity_ids: dict[str, str] = {}
        entity_rows = []
        for entity in resolution.get("entities", []):
            candidate = entity.get("candidate_entity_id")
            eid = _uuid(candidate) if candidate else deterministic_entity_id(run_id, entity["id"])
            entity_ids[entity["id"]] = eid
            if not candidate:
                entity_rows.append((eid, ctx.novel.id, generation_id, entity["kind"], entity["canonical"], chapter))
        if entity_rows:
            await _executemany(ctx.db,
                """INSERT INTO entity (id,novel_id,record_generation_id,kind,canonical,first_seen_chapter)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", entity_rows)
        # Aliases are chapter-indexed and immutable. Existing aliases in a generation
        # may be reused only after the candidate snapshot allowed that entity.
        alias_rows = []
        for entity in resolution.get("entities", []):
            eid = entity_ids[entity["id"]]
            alias_rows.extend((eid, surface, ctx.novel.source_lang, chapter) for surface in entity.get("names", []))
        if alias_rows:
            await _executemany(ctx.db,
                "INSERT INTO alias (entity_id,surface,lang,first_seen_chapter) VALUES (%s,%s,%s,%s) ON CONFLICT (entity_id,surface,lang) DO NOTHING",
                alias_rows)

        ref_ids: dict[str, str] = {}
        refs = resolution.get("references", [])
        for i, ref in enumerate(refs):
            rid = _ref_id(run_id, i)
            ref_ids[ref["id"]] = rid
            candidate = ref.get("candidate_entity_id")
            await ctx.db.execute(
                """INSERT INTO record_reference (id,novel_id,generation_id,run_id,surface,proposed_kind,candidate_entity_id,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (rid, ctx.novel.id, generation_id, run_id, ref.get("surface", ref.get("name", "")),
                 ref.get("refers_to"), _uuid(candidate) if candidate else None, ref.get("reason")))

        rows = [r for r in result.get("records", []) if r.get("usable")]
        parsed_by_index = {r["index"]: r for r in rows}
        built = result.get("rows") or []
        if not built:
            # Keep the representation lossless even if a caller supplied only parser
            # output; this also makes the publisher useful to resumable backfills.
            for rec in rows:
                participants, values = {}, {}
                for field, value in rec.get("fields", {}).items():
                    if not value:
                        continue
                    values[field] = value
                    if field in {"who", "speaker", "addressee", "character", "side_a", "side_b", "promiser", "promisee", "entity", "of"}:
                        participants[field] = [{"surface": n, "target": (resolution.get("name_map") or {}).get(n)} for n in value.replace("，", ",").split(",") if n.strip()]
                built.append({"record_index": rec["index"], "type": rec["type"], "evidence_ids": rec.get("evidence_ids", []), "participants": participants, "values": values})
        row_ids: dict[int, str] = {}
        for item in built:
            index = int(item["record_index"])
            rid = _row_id(run_id, index)
            row_ids[index] = rid
            rec = parsed_by_index.get(index, {})
            fields = rec.get("fields", {})
            told = str(fields.get("told", "")).strip().lower()
            qualifier = "recounted earlier" if told == "earlier" else (told or None)
            await ctx.db.execute(
                """INSERT INTO record_row (id,novel_id,generation_id,run_id,original_index,record_type,source_chapter,source_hash,valid_from_chapter,temporal_qualifier)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (rid, ctx.novel.id, generation_id, run_id, index, item["type"], chapter, source_hash, None, qualifier))
            for field, value in item.get("values", {}).items():
                await ctx.db.execute("INSERT INTO record_value (row_id,field_name,source_value) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (rid, field, value))
            for field, vals in item.get("participants", {}).items():
                for ordinal, part in enumerate(vals):
                    target = part.get("target")
                    eid = entity_ids.get(target) if target else None
                    refid = ref_ids.get(target) if target else None
                    # Name maps point at temporary e/r identifiers. Every participant
                    # must resolve to exactly one durable entity or reference row.
                    if eid is None and refid is None:
                        refid = _ref_id(run_id, len(ref_ids))
                        ref_ids[target or f"implicit:{index}:{field}:{ordinal}"] = refid
                        await ctx.db.execute("""INSERT INTO record_reference (id,novel_id,generation_id,run_id,surface,proposed_kind,reason)
                          VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (refid,ctx.novel.id,generation_id,run_id,part.get("surface", ""),None,"missing who's-who assignment"))
                    await ctx.db.execute("""INSERT INTO record_participant (row_id,ordinal,field_name,surface,entity_id,reference_id)
                      VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (rid,ordinal,field,part.get("surface", ""),eid,refid))
            for ordinal, passage_id in enumerate(item.get("evidence_ids", [])):
                await ctx.db.execute("INSERT INTO record_evidence (row_id,run_id,passage_id,quote) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (rid,run_id,passage_id,fields.get("quote")))
            for field, source_value in item.get("values", {}).items():
                rendered = (result.get("renderings") or {}).get(f"{index}.{field}")
                if rendered:
                    await ctx.db.execute("""INSERT INTO record_rendering (row_id,field_name,target_value,provider,served_model,status,error_detail)
                      VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (row_id,field_name) DO UPDATE SET target_value=EXCLUDED.target_value,provider=EXCLUDED.provider,served_model=EXCLUDED.served_model,status=EXCLUDED.status,error_detail=EXCLUDED.error_detail""", (rid,field,rendered.get("value"),rendered.get("provider"),rendered.get("served_model"),rendered.get("status","ready"),rendered.get("error")))

        for rec in result.get("records", []):
            if rec.get("usable"):
                continue
            await ctx.db.execute("INSERT INTO record_drop (novel_id,generation_id,run_id,original_index,parsed_record,reasons) VALUES (%s,%s,%s,%s,%s,%s)", (ctx.novel.id,generation_id,run_id,rec.get("index"),Jsonb(rec),Jsonb(rec.get("check_failures") or rec.get("issues") or ["rejected"])))
        for problem in result.get("problems", []):
            await ctx.db.execute("INSERT INTO record_drop (novel_id,generation_id,run_id,original_index,malformed_fragment,reasons) VALUES (%s,%s,%s,NULL,%s,%s)", (ctx.novel.id,generation_id,run_id,str(problem.get("raw", ""))[:400],Jsonb([problem.get("problem", "malformed discovery")])) )

        # Display occurrences: where a rendered mention sits, and which entity it renders.
        # Alignment says which source mention an occurrence corresponds to; it never
        # decides identity, so a span binds only when the authoritative resolution
        # already owns that surface. Anything else is published unbound (§0.3).
        await ctx.db.execute("DELETE FROM record_mention_binding WHERE run_id=%s", (run_id,))
        display_text = state.translation or state.envelope.raw_text
        resolution_map = (result.get("resolution") or {}).get("name_map") or {}
        for span in getattr(state, "display_spans", []) or []:
            bound = _display_binding_id(
                span.alias_id, display_text, span, getattr(state, "term_renderings", []) or [],
                resolution_map, entity_ids,
            )
            await ctx.db.execute(
                """INSERT INTO record_mention_binding
                   (novel_id,generation_id,run_id,entity_id,source_chapter,char_start,char_end)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (ctx.novel.id, generation_id, run_id, bound,
                 chapter, span.char_start, span.char_end))

        await ctx.db.execute("DELETE FROM chunk WHERE novel_id=%s AND chapter_index=%s", (ctx.novel.id, chapter))
        if target_chunks:
            await _executemany(ctx.db, "INSERT INTO chunk (novel_id,chapter_index,text,embedding) VALUES (%s,%s,%s,%s)", [(ctx.novel.id,chapter,c.text,e) for c,e in zip(target_chunks,embeddings)])
        # Each unusable candidate becomes one record_drop row, whether parsing rejected
        # it or deterministic grounding did. Counting only check_records() drops hid the
        # nine parser-level failures that motivated this contract repair.
        warning_count = _warning_count(result)
        await ctx.db.execute("""UPDATE record_run SET status='published',warning_count=%s,diagnostics=%s,
                    publication_version=COALESCE((SELECT max(publication_version)+1 FROM record_run WHERE novel_id=%s AND generation_id=%s),1),published_at=now()
                    WHERE id=%s""", (warning_count, Jsonb({"warnings": warning_count}), ctx.novel.id, generation_id, run_id))
    log.info("published records novel=%s chapter=%s rows=%s", ctx.novel.id, state.envelope.chapter_index, len(row_ids))


async def publish_fact_first(ctx: StageContext, state: PipelineState, result: dict[str, Any]) -> None:
    """Publish validated fact-first rows without coercing them into typed records."""
    run_id = result.get("fact_first_run_id")
    if not run_id:
        raise ValueError("fact-first result has no durable run id")
    async with ctx.db.transaction():
        await verify_generation(ctx, state)
        existing = await (await ctx.db.execute(
            "SELECT status,source_hash FROM fact_first_run WHERE id=%s FOR UPDATE", (run_id,))).fetchone()
        if not existing or existing[1] != state.envelope.source_meta.raw_hash:
            raise RuntimeError("fact-first run input changed inside an immutable generation")
        if existing[0] == "published":
            return
        generation_id = state.record_generation_id
        chapter = state.envelope.chapter_index
        resolution = result.get("resolution") or {}
        proposal_map = {str(k): str(v) for k, v in (resolution.get("proposal_map") or {}).items()}
        resolver_entity_ids: dict[str, str] = {}
        entity_rows = []
        for entity in resolution.get("entities", []):
            proposal = str(entity.get("id", ""))
            candidate = entity.get("candidate_entity_id")
            durable = _uuid(candidate) if candidate else deterministic_entity_id(run_id, proposal)
            resolver_entity_ids[proposal] = durable
            if not candidate:
                entity_rows.append((durable, ctx.novel.id, generation_id, entity.get("kind", "unknown"),
                                    entity.get("canonical", ""), chapter))
        # Bind normalized proposal IDs structurally. The resolver may choose different
        # local labels; proposal_map is the only accepted bridge between the two.
        if entity_rows:
            await _executemany(ctx.db,
                """INSERT INTO entity (id,novel_id,record_generation_id,kind,canonical,first_seen_chapter)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", entity_rows)
        aliases = [(resolver_entity_ids[str(entity.get("id", ""))], name, ctx.novel.source_lang,
                    chapter, generation_id)
                   for entity in resolution.get("entities", [])
                   for name in entity.get("names", []) if str(entity.get("id", "")) in resolver_entity_ids]
        if aliases:
            await _executemany(ctx.db,
                """INSERT INTO alias(entity_id,surface,lang,first_seen_chapter,record_generation_id)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", aliases)
        for proposal in result.get("accepted", {}).get("entities", result.get("entities", [])):
            await ctx.db.execute(
                """INSERT INTO fact_first_entity_proposal
                   (run_id,proposal_id,source_name,aliases,kind,english_name,passage_ids,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run_id, proposal.get("local_id", ""), proposal.get("canonical_source", ""),
                 Jsonb(proposal.get("source_aliases", [])), proposal.get("kind"),
                 proposal.get("english_name"), Jsonb(proposal.get("evidence_ids", [])), Jsonb(proposal)))
        proposal_entity_ids = {
            proposal_id: resolver_entity_ids[resolver_id]
            for proposal_id, resolver_id in proposal_map.items()
            if resolver_id in resolver_entity_ids
        }
        for proposal, durable in proposal_entity_ids.items():
            await ctx.db.execute(
                "UPDATE fact_first_entity_proposal SET persistent_entity_id=%s,resolution_status='resolved' WHERE run_id=%s AND proposal_id=%s",
                (durable, run_id, proposal))
        # DISPLAY_SCAN is deliberately rerun after identity publication for newly
        # introduced aliases: the pre-publication scan cannot see aliases that did not
        # exist in the database yet. It still only emits cards for the authoritative
        # who's-who bindings, never for a spelling match without one.
        display_text = state.translation or state.envelope.raw_text
        resolution_map = resolution.get("name_map") or {}
        scan_aliases = [Alias(alias_id=str(entity.get("id", "")), surface=name)
                        for entity in resolution.get("entities", [])
                        for name in entity.get("names", [])]
        spans = list(getattr(state, "display_spans", []) or [])
        if not spans and display_text == state.envelope.raw_text and scan_aliases:
            spans = [span for span in scan_mentions(MentionScanRequest(
                text=display_text, aliases=scan_aliases, lang=ctx.novel.source_lang)).spans
                     if whole_name(display_text, span, ctx.novel.source_lang)]
        # DISPLAY_SCAN may already have written these derived rows before identity
        # publication. Replace the chapter slice once so hovercards never duplicate.
        await ctx.db.execute("DELETE FROM mention_span WHERE novel_id=%s AND chapter_index=%s",
                             (ctx.novel.id, chapter))
        await ctx.db.execute("DELETE FROM record_mention_binding WHERE run_id=%s",
                             (_run_id(str(generation_id), chapter),))
        for span in spans:
            local = str(getattr(span, "alias_id", ""))
            display_term = display_text[span.char_start:span.char_end]
            aligned = [rendering for rendering in (getattr(state, "term_renderings", []) or [])
                       if rendering.char_start == span.char_start
                       and rendering.char_end == span.char_end
                       and rendering.display_term == display_term]
            proposal_ids = {resolution_map.get(rendering.source_term) for rendering in aligned}
            proposal_ids.discard(None)
            if len(proposal_ids) == 1:
                local = next(iter(proposal_ids))
            # DISPLAY_SCAN can carry an already durable UUID. Keep it only when the
            # current who's-who result authorizes that same UUID; alignment above still
            # wins for every span, including spans emitted before identity publication.
            durable = resolver_entity_ids.get(resolution_map.get(local, local))
            if durable is None and local in resolver_entity_ids.values():
                durable = local
            await ctx.db.execute(
                """INSERT INTO mention_span(novel_id,chapter_index,entity_id,char_start,char_end)
                   VALUES (%s,%s,%s,%s,%s)""",
                (ctx.novel.id, chapter, durable, span.char_start, span.char_end))
            if durable is None:
                continue
            await ctx.db.execute(
                """INSERT INTO record_mention_binding
                   (novel_id,generation_id,run_id,entity_id,source_chapter,char_start,char_end)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (ctx.novel.id, generation_id, _run_id(str(generation_id), chapter), durable,
                 chapter, span.char_start, span.char_end))
        for ref in result.get("unresolved_references", result.get("references", [])):
            await ctx.db.execute(
                """INSERT INTO fact_first_reference
                   (run_id,reference_id,assertion_id,surface,refers_to,candidate_proposal_id,reason,claim_id,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run_id, ref.get("local_id", ""), None, ref.get("surface", ""),
                 ref.get("refers_to"), ref.get("candidate_id"), ref.get("reason"),
                 ref.get("claim_id"), Jsonb(ref)))
        for account in (result.get("assertion_accounting") or {}).get("assertions", []):
            outcome = account.get("outcome", "omitted")
            status = account.get("status", outcome)
            await ctx.db.execute(
                """INSERT INTO fact_first_assertion
                   (run_id,assertion_id,candidate_id,statement,outcome,status,reason,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run_id, account.get("assertion_id", ""), account.get("claim_id", ""),
                 account.get("statement", ""), outcome, status,
                 account.get("reason", ""), Jsonb(account)))
        for ref in result.get("unresolved_references", result.get("references", [])):
            assertion_id = ref.get("assertion_id")
            if assertion_id:
                await ctx.db.execute(
                    "UPDATE fact_first_reference SET assertion_id=%s WHERE run_id=%s AND reference_id=%s",
                    (assertion_id, run_id, ref.get("local_id", "")))
        evidence = lambda row: Jsonb(row.get("evidence", row.get("evidence_ids", [])))
        for index, row in enumerate(result.get("accepted", {}).get("facts", []), 1):
            await ctx.db.execute(
                """INSERT INTO fact_first_fact
                   (run_id,local_id,assertion_id,subject_ref,attribute,value,source_value,polarity,attribution,condition,temporal,source_chapter,valid_from_chapter,evidence,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)""",
                (run_id, f"f{index}", row.get("assertion_id"), row.get("subject_id", ""),
                 row.get("attribute", ""), row.get("value", ""), row.get("source_span"),
                 row.get("polarity"), row.get("attribution"), row.get("condition"), row.get("temporal"),
                 state.envelope.chapter_index, evidence(row), Jsonb(row)))
        for index, row in enumerate(result.get("accepted", {}).get("relations", []), 1):
            await ctx.db.execute(
                """INSERT INTO fact_first_relation
                   (run_id,local_id,assertion_id,src_ref,dst_ref,relation,source_value,polarity,attribution,condition,temporal,source_chapter,valid_from_chapter,evidence,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)""",
                (run_id, f"r{index}", row.get("assertion_id"), row.get("src_id", ""), row.get("dst_id", ""),
                 row.get("relation", ""), row.get("source_span"), row.get("polarity"), row.get("attribution"),
                 row.get("condition"), row.get("temporal"), state.envelope.chapter_index, evidence(row), Jsonb(row)))
        for index, row in enumerate(result.get("accepted", {}).get("events", []), 1):
            await ctx.db.execute(
                """INSERT INTO fact_first_event
                   (run_id,local_id,assertion_id,action,arguments,source_value,polarity,attribution,condition,temporal,source_chapter,valid_from_chapter,evidence,payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)""",
                (run_id, f"e{index}", row.get("assertion_id"), row.get("action", ""), Jsonb(row.get("arguments", [])),
                 row.get("source_span"), row.get("polarity"), row.get("attribution"), row.get("condition"),
                 row.get("temporal"), state.envelope.chapter_index, evidence(row), Jsonb(row)))
        for output_id, rendering in (result.get("renderings") or {}).items():
            if not isinstance(rendering, dict):
                continue
            await ctx.db.execute(
                """INSERT INTO fact_first_rendering
                   (run_id,assertion_id,output_kind,output_id,target_value,provider,served_model,status,error_detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run_id, rendering.get("assertion_id"), rendering.get("output_kind", "native_assertion"),
                 str(output_id), rendering.get("target_value"),
                 rendering.get("provider"), rendering.get("served_model"),
                 rendering.get("status", "failed"), rendering.get("error_detail")))
        # Chunk vectors are optional retrieval data. Persist readable chunks even when
        # embeddings are disabled or unavailable; fact-first publication must never
        # depend on an embedding provider.
        await ctx.db.execute("DELETE FROM chunk WHERE novel_id=%s AND chapter_index=%s",
                             (ctx.novel.id, chapter))
        chunks = list(getattr(state, "chunks", []) or [])
        if chunks:
            async with ctx.db.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO chunk(novel_id,chapter_index,text,embedding) VALUES (%s,%s,%s,NULL)",
                    [(ctx.novel.id, chapter, chunk.text) for chunk in chunks])
        # The existing reader status surface is keyed by record_run. Keep it in lock
        # step with the native run after all fact-first rows are durable; otherwise the
        # next chronological chapter remains fenced behind a permanently processing
        # companion row.
        companion = _run_id(str(generation_id), chapter)
        await ctx.db.execute(
            """UPDATE record_run SET status='published',served_model=%s,
                 publication_version=COALESCE((SELECT max(publication_version)+1 FROM record_run
                   WHERE novel_id=%s AND generation_id=%s),1),published_at=now()
                WHERE id=%s AND status <> 'published'""",
            (result.get("served_model"), ctx.novel.id,
             generation_id, companion))
        await ctx.db.execute(
            """UPDATE fact_first_run SET status='published',published_at=now(),served_model=%s,
               diagnostics=jsonb_set(COALESCE(diagnostics,'{}'::jsonb), '{stages}',
                 COALESCE(diagnostics->'stages','{}'::jsonb) || %s::jsonb, true) || %s::jsonb
               WHERE id=%s""",
            (result.get("served_model"), Jsonb({"publication": "completed"}), Jsonb({"resolution": resolution, "published_counts": {
                "facts": len(result.get("accepted", {}).get("facts", [])),
                "relations": len(result.get("accepted", {}).get("relations", [])),
                "events": len(result.get("accepted", {}).get("events", [])),
                "rejected": len(result.get("rejected", [])),
            }}), run_id))
    log.info("published fact-first novel=%s chapter=%s", ctx.novel.id, state.envelope.chapter_index)
