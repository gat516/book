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
from typing import Any

from pgvector.psycopg import register_vector_async
from psycopg.types.json import Jsonb

from pipeline.context import PipelineState, StageContext
from pipeline.jobs import model_for_stage
from pipeline.passages import source_passages
from pipeline.records import deterministic_entity_id

log = logging.getLogger(__name__)

CHECKS_VERSION = "records-checks-v1"
PARSER_VERSION = "records-parser-v1"


def _uuid(value: str) -> str:
    return str(uuid.UUID(value))


async def _generation(ctx: StageContext, *, source_hash: str) -> tuple[str, dict[str, Any]]:
    """Lock and return the active generation, replacing an uncommitted seed when needed."""
    db = ctx.db
    await db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"records:{ctx.novel.id}",))
    row = await (await db.execute(
        """SELECT n.active_record_generation::text,g.id::text,g.extraction_model,
                  g.prompt_version,g.checks_version,g.source_lang,g.target_lang,g.state
             FROM novel n LEFT JOIN record_generation g ON g.id=n.active_record_generation
            WHERE n.id=%s FOR UPDATE""", (ctx.novel.id,))).fetchone()
    if row is None:
        raise RuntimeError("novel disappeared while publishing records")
    active, gid, model, prompt, checks, source_lang, target_lang, state = row
    requested_model = model_for_stage("extract", ctx.cfg, ctx.model_override)
    mismatch = (not gid or state == "retired" or
                (model and model != requested_model) or
                (prompt and prompt != ctx.cfg.prompt_version) or
                (checks and checks != CHECKS_VERSION) or
                source_lang != ctx.novel.source_lang or target_lang != ctx.novel.target_lang)
    # A blank generation is the migration's seed. It is safe to pin it to the first
    # actual request; once a run exists, every changed input receives a new generation.
    if gid and not mismatch:
        used = await (await db.execute(
            "SELECT 1 FROM record_run WHERE generation_id=%s AND status IN ('published','processing','failed') LIMIT 1",
            (gid,))).fetchone()
        mismatch = bool(used and (model != requested_model or prompt != ctx.cfg.prompt_version or checks != CHECKS_VERSION))
    if not gid or mismatch:
        new = await (await db.execute(
            """INSERT INTO record_generation
              (novel_id,ontology,prompt_version,checks_version,extraction_model,rendering_model,source_lang,target_lang,state)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active') RETURNING id::text""",
            (ctx.novel.id, Jsonb(ctx.novel.ontology), ctx.cfg.prompt_version, CHECKS_VERSION,
             requested_model, None, ctx.novel.source_lang, ctx.novel.target_lang))).fetchone()
        gid = str(new[0])
        await db.execute("UPDATE novel SET active_record_generation=%s WHERE id=%s", (gid, ctx.novel.id))
        log.info("activated records generation novel=%s generation=%s", ctx.novel.id, gid)
    else:
        # Pin the effective model/config on the seed before its first run.
        await db.execute("""UPDATE record_generation SET extraction_model=%s,prompt_version=%s,
                           checks_version=%s,source_lang=%s,target_lang=%s
                         WHERE id=%s AND extraction_model=''""",
                         (requested_model, ctx.cfg.prompt_version, CHECKS_VERSION,
                          ctx.novel.source_lang, ctx.novel.target_lang, gid))
    return str(gid), {"requested_model": requested_model}


def _run_id(generation_id: str, chapter: int) -> str:
    return str(uuid.uuid5(uuid.UUID(generation_id), f"run:{chapter}"))


def _row_id(run_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), f"row:{index}"))


def _ref_id(run_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), f"reference:{index}"))


async def publish_records(ctx: StageContext, state: PipelineState) -> None:
    """Publish one complete chapter run and derived target-language chunks atomically."""
    result = state.records
    if not result:
        raise RuntimeError("records stage produced no result")
    source_hash = state.envelope.source_meta.raw_hash
    passages = source_passages(state.envelope.raw_text)
    # Embeddings are derived chapter data. They are computed before opening the
    # publication transaction so a provider failure cannot leave a half-published run.
    target_chunks = state.chunks
    embeddings = await ctx.embed_provider.embed([c.text for c in target_chunks]) if target_chunks else []
    await register_vector_async(ctx.db)

    async with ctx.db.transaction():
        generation_id, config = await _generation(ctx, source_hash=source_hash)
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
        entity_ids = {str(e["id"]) for e in (result.get("resolution") or {}).get("entities", [])}
        for span in getattr(state, "display_spans", []) or []:
            bound = str(span.alias_id) if span.alias_id else ""
            await ctx.db.execute(
                """INSERT INTO record_mention_binding
                   (novel_id,generation_id,run_id,entity_id,source_chapter,char_start,char_end)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (ctx.novel.id, generation_id, run_id, bound if bound in entity_ids else None,
                 chapter, span.char_start, span.char_end))

        await ctx.db.execute("DELETE FROM chunk WHERE novel_id=%s AND chapter_index=%s", (ctx.novel.id, chapter))
        if target_chunks:
            await ctx.db.executemany("INSERT INTO chunk (novel_id,chapter_index,text,embedding) VALUES (%s,%s,%s,%s)", [(ctx.novel.id,chapter,c.text,e) for c,e in zip(target_chunks,embeddings)])
        warning_count = len(result.get("problems", [])) + len(result.get("checks", {}).get("dropped", [])) + len((result.get("resolution") or {}).get("problems", []))
        await ctx.db.execute("""UPDATE record_run SET status='published',warning_count=%s,diagnostics=%s,
                    publication_version=COALESCE((SELECT max(publication_version)+1 FROM record_run WHERE novel_id=%s AND generation_id=%s),1),published_at=now()
                    WHERE id=%s""", (warning_count, Jsonb({"warnings": warning_count}), ctx.novel.id, generation_id, run_id))
    log.info("published records novel=%s chapter=%s rows=%s", ctx.novel.id, state.envelope.chapter_index, len(row_ids))
