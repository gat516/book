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


async def publish_records(ctx: StageContext, state: PipelineState) -> None:
    """Publish one complete chapter run and derived target-language chunks atomically."""
    result = state.records
    if not result:
        raise RuntimeError("records stage produced no result")
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
        await ctx.db.executemany(
            """INSERT INTO record_passage (novel_id,generation_id,run_id,chapter_index,passage_id,text,char_start,char_end,ordinal,source_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(ctx.novel.id, generation_id, run_id, chapter, p["id"], p["text"],
              p["char_start"], p["char_end"], i, source_hash) for i, p in enumerate(passages)])

        resolution = result.get("resolution") or {}
        entity_ids: dict[str, str] = {}
        entity_rows = []
        for entity in resolution.get("entities", []):
            candidate = entity.get("candidate_entity_id")
            eid = _uuid(candidate) if candidate else deterministic_entity_id(run_id, entity["id"])
            entity_ids[entity["id"]] = eid
            if not candidate:
                entity_rows.append((eid, ctx.novel.id, generation_id, entity["kind"], entity["canonical"], chapter))
        if entity_rows:
            await ctx.db.executemany(
                """INSERT INTO entity (id,novel_id,record_generation_id,kind,canonical,first_seen_chapter)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", entity_rows)
        # Aliases are chapter-indexed and immutable. Existing aliases in a generation
        # may be reused only after the candidate snapshot allowed that entity.
        alias_rows = []
        for entity in resolution.get("entities", []):
            eid = entity_ids[entity["id"]]
            alias_rows.extend((eid, surface, ctx.novel.source_lang, chapter) for surface in entity.get("names", []))
        if alias_rows:
            await ctx.db.executemany(
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
            await ctx.db.executemany("INSERT INTO chunk (novel_id,chapter_index,text,embedding) VALUES (%s,%s,%s,%s)", [(ctx.novel.id,chapter,c.text,e) for c,e in zip(target_chunks,embeddings)])
        warning_count = len(result.get("problems", [])) + len(result.get("checks", {}).get("dropped", [])) + len((result.get("resolution") or {}).get("problems", []))
        await ctx.db.execute("""UPDATE record_run SET status='published',warning_count=%s,diagnostics=%s,
                    publication_version=COALESCE((SELECT max(publication_version)+1 FROM record_run WHERE novel_id=%s AND generation_id=%s),1),published_at=now()
                    WHERE id=%s""", (warning_count, Jsonb({"warnings": warning_count}), ctx.novel.id, generation_id, run_id))
    log.info("published records novel=%s chapter=%s rows=%s", ctx.novel.id, state.envelope.chapter_index, len(row_ids))
