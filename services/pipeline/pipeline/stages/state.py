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

import hashlib
import logging

from pipeline.batch import BatchManager
from pipeline.context import PipelineState, StageContext
from pipeline.extraction import Extraction, build_system_prompt, build_user_prompt, parse_extraction
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


def response_cache_key(job_key: str) -> str:
    """Version prompt/schema results without replaying committed append-only facts.

    The durable job key still guards the graph write (§0.2, §6.1). Only unfinished
    work gets the new prompt; changing its result-cache key must not undo that guard.
    """
    return hashlib.sha256(f"{job_key}\x1fstate-response-v4-value-en".encode()).hexdigest()


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

        if await job_is_done(
            ctx.db,
            novel_id=envelope.novel_id,
            chapter_index=envelope.chapter_index,
            stage=STAGE,
            key=key,
        ):
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

        cache_key = response_cache_key(key)
        cached = await ctx.cache.get(cache_key)
        if cached is not None:
            try:
                extraction = parse_extraction(cached)
            except ValueError:
                # Older workers cached before validation. A retry must not replay
                # the same invalid response for the cache's entire 14-day TTL (§6.1).
                await ctx.cache.delete(cache_key)
                log.warning(
                    "stage %s chapter=%d discarded invalid cached extraction",
                    self.name,
                    envelope.chapter_index,
                )
                cached = None
            else:
                log.info("stage %s chapter=%d cache hit", self.name, envelope.chapter_index)
        if cached is None:
            # An AdmissionRejected from here propagates untouched: it is backpressure,
            # not failure, and the retry/attempts policy that must not conflate them
            # lives with the job runner (§6.2, §14.3), not in stage code.
            request: BatchRequest = {
                "id": key,
                "prompt": build_user_prompt(envelope.raw_text),
                "system": build_system_prompt(ctx.novel.ontology),
                "json_mode": True,
                "json_schema": Extraction.model_json_schema(),
                "model": model_for_stage(STAGE, ctx.cfg, ctx.model_override),
            }
            # Ollama's batch API is the protocol's sequential compatibility path. Use
            # the phase-aware streaming client there so long CPU prompt evaluation is
            # not mistaken for a provider outage at the ordinary 120s buffered-read
            # ceiling. Hosted/native batch providers retain the worker-owned manager.
            manager = (
                BatchManager(ctx.resolve_provider)
                if ctx.resolve_provider is not None
                else ctx.batch_manager
            )
            batch_id = await manager.batch_submit([request])
            results = await manager.batch_poll(batch_id)
            result = manager.require_single_result(key, results)
            raw = result["output"]
            # Invalid structured output is a failed attempt, never a reusable result.
            extraction = parse_extraction(raw)
            await ctx.cache.put(
                cache_key,
                raw,
                requested_model_id=model_id_for_stage(STAGE, ctx.cfg, ctx.provider_id, ctx.model_override),
                served_provider=result["served_provider"],
                served_model=result["served_model"],
                stage=STAGE,
            )

        state.extraction = extraction
        state.state_job_key = key

        log.info(
            "stage %s chapter=%d entities=%d facts=%d edges=%d events=%d discarded=%s",
            self.name,
            envelope.chapter_index,
            len(extraction.entities),
            len(extraction.facts),
            len(extraction.edges),
            len(extraction.events),
            extraction.discarded_rows,
        )
