"""DISPLAY SCAN (instructions.md §5 step 6; PLAN.md Phase 5.2).

Produces the mention spans the reader UI highlights and a durable source-term → exact
display-span ledger. The searchable surface set is ``glossary.target_term`` (the locked
English forms TRANSLATE primed into the prose) plus pending name choices, scanned over
the DISPLAY text -- source-text offsets do not slice a translation (§0.5).

Spans carry no entity id: identity is not decided here. Wiki pages link names to
characters (migration 0112); a span is terminology, never identity (§0.3).

No chapter gate removes glossary targets from the scan: ingestion is not a read path
(§0.3 governs reads, not writes).
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.display_names import TermRenderingOccurrence, align_names, discover_names, merge_names
from pipeline.graph import GraphWriter
from pipeline.term_choices import record_term_choices
from pipeline.mentions import Alias, MentionScanRequest, scan_mentions

log = logging.getLogger(__name__)


class DisplayScanStage:
    name = "display_scan"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        state.term_renderings = []
        await self._linked_mentions(ctx, state)
        text = state.translation if state.translation is not None else state.envelope.raw_text
        if ctx.novel.source_lang != ctx.novel.target_lang and state.translation is None:
            return
        if state.source_names_primed:
            # TRANSLATE decided this chapter's names from the source and primed them, so
            # the exact scan above already found every one. Discovery and alignment are
            # only the fallback for prose that arrived translated (bootstrap chapters).
            async with ctx.db.transaction():
                await GraphWriter(ctx.db).replace_mention_spans(
                    ctx.novel.id, state.envelope.chapter_index, state.display_spans,
                    state.term_renderings,
                )
            log.info("stage %s chapter=%d primed spans=%d", self.name,
                     state.envelope.chapter_index, len(state.display_spans))
            return
        linked = list(state.display_spans)
        discovered = await discover_names(ctx, text)
        state.display_spans = merge_names(state.display_spans, discovered)
        unlinked = [span for span in discovered if not any(
            span.char_start < old.char_end and old.char_start < span.char_end
            for old in linked)]
        state.term_renderings.extend(await align_names(
            ctx, state.envelope.raw_text, text, unlinked))
        # Cards are derived from readable prose, not gated on fact extraction completing.
        # Publish atomically here; graph-write may idempotently replace the same spans.
        async with ctx.db.transaction():
            await record_term_choices(ctx, state, state.term_renderings)
            await GraphWriter(ctx.db).replace_mention_spans(
                ctx.novel.id, state.envelope.chapter_index, state.display_spans,
                state.term_renderings,
            )
        log.info("stage %s chapter=%d linked=%d unlinked=%d", self.name,
                 state.envelope.chapter_index,
                 sum(bool(span.alias_id) for span in state.display_spans),
                 sum(not span.alias_id for span in state.display_spans))

    async def _linked_mentions(self, ctx: StageContext, state: PipelineState) -> None:
        # Same-language novels display their source text unchanged.
        text = state.envelope.raw_text if ctx.novel.source_lang == ctx.novel.target_lang \
            else state.translation
        if text is None:
            # TRANSLATE was skipped or hasn't run; leave nothing to highlight rather than
            # scan the wrong text.
            return

        rows = await (
            await ctx.db.execute(
                "SELECT source_term, target_term FROM glossary "
                "WHERE novel_id = %s AND NOT deleted AND locked_at_chapter <= %s",
                (ctx.novel.id, state.envelope.chapter_index),
            )
        ).fetchall()
        # Pending choices are primed into translation exactly like locked terms, so their
        # spellings are in the prose verbatim. Only terms this chapter's source contains:
        # an unrelated phrase that happens to match a pending spelling is not a mention.
        locked = {source for source, _ in rows}
        pending = await (await ctx.db.execute(
            "SELECT source_term, candidates->0->>'target_term' FROM character_name_review "
            "WHERE novel_id=%s AND status='pending' AND first_seen_chapter <= %s "
            "AND jsonb_array_length(candidates) > 0",
            (ctx.novel.id, state.envelope.chapter_index),
        )).fetchall()
        rows = list(rows) + [(source, target) for source, target in pending
                             if source not in locked and target and source in state.envelope.raw_text]
        # One search per spelling. Two source terms can share a spelling (a traditional/
        # variant pair, or a name and a longer title built on it); scanning it twice would
        # put two spans on the same text. A locked term comes first, so it keeps the span.
        spelled: set[str] = set()
        rows = [(source, target) for source, target in rows
                if not (target in spelled or spelled.add(target))]

        request = MentionScanRequest(
            text=text,
            aliases=[
                Alias(alias_id=str(index), surface=target_term)
                for index, (_, target_term) in enumerate(rows)
            ],
            lang=ctx.novel.target_lang,
        )
        response = await ctx.textproc.scan(request) if ctx.textproc else scan_mentions(request)
        state.display_spans = []
        for span in response.spans:
            source_term, target_term = rows[int(span.alias_id)]
            # An empty alias id is stored as a NULL entity: spans are terminology only.
            state.display_spans.append(span.model_copy(update={"alias_id": ""}))
            state.term_renderings.append(TermRenderingOccurrence(
                source_term, target_term, span.char_start, span.char_end, "glossary"))

        log.debug(
            "stage %s chapter=%d terms=%d spans=%d",
            self.name,
            state.envelope.chapter_index,
            len(rows),
            len(state.display_spans),
        )
