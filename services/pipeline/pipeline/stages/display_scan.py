"""Stage 6: DISPLAY SCAN (instructions.md §5 step 6; PLAN.md Phase 5.2).

Produces the mention spans the reader UI highlights. These are a **separate** pass from
the extraction-time scan (``ScanStage``, step 2): that pass finds aliases in the SOURCE
text so RESOLVE can bind them; this one finds the LOCKED GLOSSARY TERMS in the DISPLAY
text so the web client can highlight them. When the novel is translated, the two texts
are different strings in different scripts — source-text offsets do not slice the
translated text (§0.5, §5 step 6), so reusing ``state.mentions`` there would highlight
nonsense ranges.

``glossary.target_term`` (not ``entity``/``alias``) is the alias set here, because it is
exactly "the locked English surface forms" — the same discipline TRANSLATE relies on to
keep terms stable chapter to chapter. Rows are keyed by ``entity_id`` for the same reason
``ScanStage`` keys by it (§4: alias has no surrogate key); a glossary row with a NULL
entity_id would mean RESOLVE never bound it, which both of ``resolve.py``'s glossary-lock
call sites prevent by construction, so it is filtered defensively rather than assumed.

No chapter gate on the glossary query, for the same reason ``ScanStage`` has none:
ingestion is not a read path (§0.3 governs reads, not writes).
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.mentions import Alias, MentionScanRequest, scan_mentions

log = logging.getLogger(__name__)


class DisplayScanStage:
    name = "display_scan"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        if ctx.novel.source_lang == ctx.novel.target_lang:
            # Displayed text IS the source text, unchanged — the step-2 scan already
            # computed correct offsets against it. Re-scanning would be redundant work
            # producing an identical result.
            state.display_spans = state.mentions
            return

        if state.translation is None:
            # TRANSLATE was skipped or hasn't run (shouldn't happen once source_lang !=
            # target_lang is pinned, but leave nothing to highlight rather than scan the
            # wrong text).
            return

        rows = await (
            await ctx.db.execute(
                "SELECT entity_id, target_term FROM glossary WHERE novel_id = %s",
                (ctx.novel.id,),
            )
        ).fetchall()

        request = MentionScanRequest(
            text=state.translation,
            aliases=[
                Alias(alias_id=str(entity_id), surface=target_term)
                for entity_id, target_term in rows
                if entity_id is not None
            ],
            lang=ctx.novel.target_lang,
        )
        response = await ctx.textproc.scan(request) if ctx.textproc else scan_mentions(request)
        state.display_spans = response.spans

        log.debug(
            "stage %s chapter=%d terms=%d spans=%d",
            self.name,
            state.envelope.chapter_index,
            len(rows),
            len(state.display_spans),
        )
