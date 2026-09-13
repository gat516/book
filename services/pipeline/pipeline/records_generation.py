"""Generation preparation and publication fences for the records pipeline.

Identity resolution is generation-scoped. A worker must choose the generation before
candidate selection and may publish only if that same generation is still active when
the chapter transaction commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import hashlib
import json
import uuid

from psycopg.types.json import Jsonb

from pipeline.context import PipelineState, StageContext
from pipeline.jobs import model_for_stage
from pipeline.records_prompts import PROMPT_CONTRACT_VERSION, RESOLVE_SYSTEM, RENDER_SYSTEM
from pipeline.fact_first import DISCOVERY_ATOMIC_SYSTEM, NORMALIZATION_ASSERTION_SYSTEM
from pipeline.benchmark_selection import SELECTION_SYSTEM, NORMALIZATION_SELECTION_SYSTEM

CHECKS_VERSION = "fact-first-checks-v1"


class GenerationFenceError(RuntimeError):
    """The chapter was extracted against a generation that cannot safely publish."""


@dataclass(frozen=True)
class GenerationPin:
    id: str
    requested_model: str
    prompt_version: str
    checks_version: str
    source_lang: str
    target_lang: str


def _requested(ctx: StageContext) -> GenerationPin:
    # §0: a provider, prompt, or budget change opens a new immutable extraction
    # generation, even if an operator forgot to bump the descriptive version.
    contract = {
        "baseline": "96ff9cf", "variants": ["atomic", "compact", "assertion"],
        "systems": [DISCOVERY_ATOMIC_SYSTEM, SELECTION_SYSTEM,
                    NORMALIZATION_ASSERTION_SYSTEM, NORMALIZATION_SELECTION_SYSTEM,
                    RESOLVE_SYSTEM, RENDER_SYSTEM],
        "provider": ctx.provider_id or ctx.cfg.llm_provider,
        "max_output_tokens": ctx.cfg.hosted_graph_output_tokens,
        "reasoning_effort": "low",
    }
    fingerprint = hashlib.sha256(json.dumps(
        contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()
    return GenerationPin(
        id="",
        requested_model=model_for_stage("extract", ctx.cfg, ctx.model_override),
        prompt_version=f"{ctx.cfg.prompt_version}:{PROMPT_CONTRACT_VERSION}:{fingerprint}",
        checks_version=CHECKS_VERSION,
        source_lang=ctx.novel.source_lang,
        target_lang=ctx.novel.target_lang,
    )


def _run_id(generation_id: str, chapter: int) -> str:
    return str(uuid.uuid5(uuid.UUID(generation_id), f"run:{chapter}"))


async def mark_record_processing(ctx: StageContext, state: PipelineState) -> bool:
    """Expose a pinned chapter as processing before identity resolution starts.

    The row is intentionally created with a deterministic preliminary request identity;
    publication replaces it with the full extraction identity.  A published run is
    immutable and is never moved back to processing.
    """
    generation_id = state.record_generation_id
    if not generation_id:
        raise GenerationFenceError("records state has no generation pin")
    chapter = state.envelope.chapter_index
    source_hash = state.envelope.source_meta.raw_hash
    request_identity = hashlib.sha256(
        f"records-processing:{generation_id}:{chapter}:{source_hash}".encode()
    ).hexdigest()
    run_id = _run_id(generation_id, chapter)
    cursor = await ctx.db.execute(
        """INSERT INTO record_run
             (id,novel_id,generation_id,chapter_index,source_hash,request_identity,
              extraction_model,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'processing')
           ON CONFLICT (novel_id,generation_id,chapter_index) DO UPDATE SET
             status='processing', source_hash=EXCLUDED.source_hash,
             request_identity=EXCLUDED.request_identity,
             extraction_model=EXCLUDED.extraction_model
           WHERE record_run.status <> 'published'
             AND record_run.source_hash = EXCLUDED.source_hash""",
        (run_id, ctx.novel.id, generation_id, chapter, source_hash,
         request_identity, state.record_generation_config["requested_model"]),
    )
    # A different source hash means an immutable run already exists for this chapter.
    # Do not silently continue extraction with no progress row; publication would only
    # discover the conflict after spending the model budget.
    if cursor.rowcount == 0:
        existing = await (await ctx.db.execute(
            "SELECT status,source_hash FROM record_run "
            "WHERE novel_id=%s AND generation_id=%s AND chapter_index=%s",
            (ctx.novel.id, generation_id, chapter),
        )).fetchone()
        if existing and existing[0] == "published" and existing[1] == source_hash:
            return False
        raise GenerationFenceError(
            "record chapter input changed inside an existing generation; retry after rebuild"
        )
    return True


async def prepare_generation(ctx: StageContext, state: PipelineState) -> GenerationPin:
    """Pin the active generation before records candidates are selected.

    A blank seed created by migration/rebuild is initialized in place. A generation
    already used by a published/processing/failed run is immutable: changed prompts,
    ontology, model, or language are a bounded fence error instead of silently moving
    this chapter into a different generation.
    """
    requested = _requested(ctx)
    async with ctx.db.transaction():
        await ctx.db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"records:{ctx.novel.id}",),
        )
        row = await (await ctx.db.execute(
            """SELECT n.active_record_generation::text,g.state,g.ontology,
                      g.extraction_model,g.prompt_version,g.checks_version,
                      g.source_lang,g.target_lang
                 FROM novel n LEFT JOIN record_generation g
                   ON g.id=n.active_record_generation
                WHERE n.id=%s FOR UPDATE OF n""",
            (ctx.novel.id,),
        )).fetchone()
        if row is None:
            raise GenerationFenceError("novel disappeared while preparing records generation")
        gid, generation_state, ontology, model, prompt, checks, source_lang, target_lang = row
        expected = getattr(state, "expected_record_generation_id", None)
        if expected and (not gid or str(gid) != expected):
            raise GenerationFenceError(
                "queued records pointer belongs to a retired generation; discard the stale work"
            )
        if not gid:
            new = await (await ctx.db.execute(
                """INSERT INTO record_generation
                   (novel_id,ontology,prompt_version,checks_version,extraction_model,
                    rendering_model,source_lang,target_lang,state)
                   VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,'active')
                   RETURNING id::text""",
                (ctx.novel.id, Jsonb(ctx.novel.ontology), requested.prompt_version,
                 requested.checks_version, requested.requested_model,
                 requested.source_lang, requested.target_lang),
            )).fetchone()
            gid = str(new[0])
            await ctx.db.execute(
                "UPDATE novel SET active_record_generation=%s WHERE id=%s",
                (gid, ctx.novel.id),
            )
        else:
            used = await (await ctx.db.execute(
                "SELECT EXISTS (SELECT 1 FROM record_run WHERE generation_id=%s "
                "AND status IN ('published','processing','failed'))",
                (gid,),
            )).fetchone()
            blank = not model
            compatible = (
                generation_state == "active"
                and ontology == ctx.novel.ontology
                and model == requested.requested_model
                and prompt == requested.prompt_version
                and checks == requested.checks_version
                and source_lang == requested.source_lang
                and target_lang == requested.target_lang
            )
            if not compatible and used and used[0]:
                raise GenerationFenceError(
                    "active records generation is already used with incompatible config; "
                    "rebuild records before retrying this chapter"
                )
            if not compatible or blank:
                await ctx.db.execute(
                    """UPDATE record_generation
                          SET ontology=%s,prompt_version=%s,checks_version=%s,
                              extraction_model=%s,source_lang=%s,target_lang=%s,state='active'
                        WHERE id=%s""",
                    (Jsonb(ctx.novel.ontology), requested.prompt_version,
                     requested.checks_version, requested.requested_model,
                     requested.source_lang, requested.target_lang, gid),
                )

        # Rebuilds are chronological because who's-who only sees earlier published
        # identities. Do not accept chapter N while a lower saved/translated chapter
        # in this generation is still missing a published run.
        missing = await (await ctx.db.execute(
            """SELECT 1
                 FROM chapter c
                WHERE c.novel_id=%s AND c.chapter_index < %s
                  AND c.translation_ready
                  AND NOT EXISTS (
                    SELECT 1 FROM record_run r
                     WHERE r.novel_id=c.novel_id AND r.generation_id=%s
                       AND r.chapter_index=c.chapter_index AND r.status='published'
                  )
                LIMIT 1""",
            (ctx.novel.id, state.envelope.chapter_index, gid),
        )).fetchone()
        if missing:
            raise GenerationFenceError(
                f"chapter {state.envelope.chapter_index} is out of order; earlier records are unpublished"
            )
    pin = GenerationPin(
        id=str(gid), requested_model=requested.requested_model,
        prompt_version=requested.prompt_version, checks_version=requested.checks_version,
        source_lang=requested.source_lang, target_lang=requested.target_lang,
    )
    state.record_generation_id = pin.id
    state.record_generation_config = {
        "ontology": ctx.novel.ontology,
        "requested_model": pin.requested_model,
        "prompt_version": pin.prompt_version,
        "checks_version": pin.checks_version,
        "source_lang": pin.source_lang,
        "target_lang": pin.target_lang,
    }
    return pin


async def verify_generation(ctx: StageContext, state: PipelineState) -> dict[str, Any]:
    """Lock and verify the exact generation selected before records extraction."""
    generation_id = state.record_generation_id
    if not generation_id:
        raise GenerationFenceError("records state has no generation pin")
    requested = _requested(ctx)
    await ctx.db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"records:{ctx.novel.id}",),
    )
    row = await (await ctx.db.execute(
        """SELECT n.active_record_generation::text,g.state,g.ontology,
                  g.extraction_model,g.prompt_version,g.checks_version,
                  g.source_lang,g.target_lang
             FROM novel n JOIN record_generation g
               ON g.id=n.active_record_generation
            WHERE n.id=%s FOR UPDATE""",
        (ctx.novel.id,),
    )).fetchone()
    if row is None or str(row[0]) != str(generation_id):
        raise GenerationFenceError("records generation changed while chapter was extracting; retry after rebuild")
    _, generation_state, ontology, model, prompt, checks, source_lang, target_lang = row
    pinned = state.record_generation_config
    if (
        generation_state != "active"
        or ontology != ctx.novel.ontology
        or (pinned is not None and ontology != pinned.get("ontology"))
        or model != requested.requested_model
        or prompt != requested.prompt_version
        or checks != requested.checks_version
        or source_lang != requested.source_lang
        or target_lang != requested.target_lang
    ):
        raise GenerationFenceError("records generation config changed while chapter was extracting; retry after rebuild")
    return {"requested_model": requested.requested_model}
