"""Stage 2: MENTION SCAN (instructions.md §5 step 2; PLAN.md 1.6).

Finds where the novel's *already-known* aliases appear in this chapter, so RESOLVE gets
its cheap high-confidence hits without asking a model anything. Everything the alias set
does not already contain is invisible here by construction — discovering genuinely new
names is resolve's proposal pass, not the scanner's job (§5).

The matching itself lives in ``mentions.py``, shaped from ``proto/textproc.proto``. In
Phase 4 that call becomes gRPC to the Rust ``textproc`` service and this file does not
change.

``alias_id`` is opaque to the scanner — it echoes back whatever the caller supplied — so
we put the **entity_id** there. That is the thing resolution actually binds to, and the
matched surface is recoverable from the span offsets anyway. An alias has no surrogate key
in this schema (its PK is ``(entity_id, surface, lang)``), so there is no better candidate.

No chapter gate on the alias query, deliberately: ingestion is not a read path. The
spoiler gate (§0.3) governs what a *reader* may see and is enforced on the way out; a
worker ingesting chapter 500 legitimately sees every alias in the novel. Gating here would
silently degrade resolution for exactly the later chapters that need it most.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.mentions import Alias, MentionScanRequest, scan_mentions

log = logging.getLogger(__name__)


class ScanStage:
    name = "scan"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        rows = await (
            await ctx.db.execute(
                """
                SELECT a.entity_id, a.surface
                FROM alias a
                JOIN entity e ON e.id = a.entity_id
                WHERE e.novel_id = %s
                """,
                (ctx.novel.id,),
            )
        ).fetchall()

        request = MentionScanRequest(
            text=state.envelope.raw_text,
            aliases=[Alias(alias_id=str(entity_id), surface=surface) for entity_id, surface in rows],
            lang=ctx.novel.source_lang,
        )
        response = await ctx.textproc.scan(request) if ctx.textproc else scan_mentions(request)
        state.mentions = response.spans

        log.debug(
            "stage %s chapter=%d aliases=%d mentions=%d",
            self.name,
            state.envelope.chapter_index,
            len(rows),
            len(state.mentions),
        )
