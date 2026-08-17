"""Stage 5: STATE EXTRACT (instructions.md §5 step 5; PLAN.md 1.5) — the first real LLM stage.

Pulls events and state changes for the ontology's tracked attributes and relations out of
one chapter, tagged with that chapter's index. It populates ``fact``/``edge``/``event``,
which is what every downstream feature reads — the reason it is built before resolve and
translate even though it *runs* after them (PLAN.md 1.5).

It writes nothing itself. It produces a validated ``Extraction`` onto ``PipelineState``;
graph-write is the only module that touches the knowledge tables (graph.py).

**Two caches, two different jobs** (§6.1, cache.py). Before doing anything this stage asks
whether its job row is already ``done`` — meaning this exact extraction was already written
to an append-only graph — and returns without an LLM call *and without output* if so.
Only then does it check Redis for a stored response, which saves the call but still lets
the write happen. Skipping one check or the other looks like the same optimization and
is not: the first protects the graph from duplicate facts, the second protects the bill.

The chapter text goes in the user block and never ahead of the system block, because the
system block (instructions + ontology) is the stable prefix providers cache on (§6.2).

Failover is fine here and is deliberately not pinned (``pin_model`` stays False): this
stage's output is structured data, where a different model is a quality variance, not a
discontinuity. Translate is the opposite case and pins (§14/§15.4). If the response comes
back served by some other model, the cache declines to store it rather than mis-attribute
it (cache.py).
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.extraction import build_system_prompt, build_user_prompt, parse_extraction
from pipeline.jobs import (
    idempotency_key,
    insert_job,
    job_is_done,
    model_for_stage,
    model_id_for_stage,
)
from pipeline.llm.provider import BatchRequest

log = logging.getLogger(__name__)

STAGE = "state"


class StateStage:
    name = STAGE

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        envelope = state.envelope
        key = idempotency_key(
            STAGE,
            envelope.source_meta.raw_hash,
            ctx.cfg,
            ontology=ctx.novel.ontology,
        )

        if await job_is_done(ctx.db, key):
            # Already written to the graph. Leaving ``state.extraction`` as None is what
            # tells graph-write to write nothing — re-inserting would duplicate every
            # fact, because the knowledge tables are append-only (§0.2).
            log.info(
                "stage %s chapter=%d already done (job %s); skipping",
                self.name,
                envelope.chapter_index,
                key[:12],
            )
            return

        await insert_job(
            ctx.db,
            novel_id=envelope.novel_id,
            chapter_index=envelope.chapter_index,
            stage=STAGE,
            key=key,
        )

        cached = await ctx.cache.get(key)
        if cached is not None:
            log.info("stage %s chapter=%d cache hit", self.name, envelope.chapter_index)
            raw = cached
        else:
            # An AdmissionRejected from here propagates untouched: it is backpressure,
            # not failure, and the retry/attempts policy that must not conflate them
            # lives with the job runner (§6.2, §14.3), not in stage code.
            request: BatchRequest = {
                "id": key,
                "prompt": build_user_prompt(envelope.raw_text),
                "system": build_system_prompt(ctx.novel.ontology),
                "json_mode": True,
                "model": model_for_stage(STAGE, ctx.cfg),
            }
            batch_id = await ctx.batch_manager.batch_submit([request])
            results = await ctx.batch_manager.batch_poll(batch_id)
            result = ctx.batch_manager.require_single_result(key, results)
            raw = result["output"]
            await ctx.cache.put(
                key,
                raw,
                requested_model_id=model_id_for_stage(STAGE, ctx.cfg),
                served_provider=result["served_provider"],
                served_model=result["served_model"],
                stage=STAGE,
            )

        extraction = parse_extraction(raw)
        state.extraction = extraction
        state.state_job_key = key

        log.info(
            "stage %s chapter=%d entities=%d facts=%d edges=%d events=%d",
            self.name,
            envelope.chapter_index,
            len(extraction.entities),
            len(extraction.facts),
            len(extraction.edges),
            len(extraction.events),
        )
