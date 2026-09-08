"""Stage 6: DISPLAY SCAN (instructions.md §5 step 6; PLAN.md Phase 5.2).

Produces the mention spans the reader UI highlights and a durable source-term → exact
display-span ledger. These are a **separate** pass from
the extraction-time scan (``ScanStage``, step 2): that pass finds aliases in the SOURCE
text so RESOLVE can bind them; this one finds locked glossary links in the DISPLAY
text and independently discovers named mentions that have no identity yet. For translated
novels, these are different strings in different scripts — source-text offsets do not slice the
translated text (§0.5, §5 step 6), so reusing ``state.mentions`` there would highlight
nonsense ranges.

``glossary.target_term`` (not ``entity``/``alias``) is the searchable surface set here,
because it is exactly "the locked English surface forms" — the same discipline TRANSLATE
relies on to keep terms stable chapter to chapter. A target may be locked before RESOLVE
has created its entity, so every target is scanned and such a span is written as a
presentation placeholder. An entity id is copied into the legacy presentation table only
when a matching ``glossary_binding`` belongs to the novel's active, trusted legacy
revision. Managed graph identity stays in revision-scoped ``display_mention``/
``mention_binding``; it must never be copied into the legacy ``mention_span`` row (§0.3).
The ledger records terminology alignment only; it never creates or binds an entity.

No chapter gate removes glossary targets from the scan, for the same reason ``ScanStage``
has none: ingestion is not a read path (§0.3 governs reads, not writes). Knowledge-time
still gates entity links: a target may receive a legacy entity id only after both its
glossary lock and its revision binding are known by the chapter being processed.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.display_names import TermRenderingOccurrence, align_names, discover_names, merge_names
from pipeline.graph import GraphWriter
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
            await GraphWriter(ctx.db).replace_mention_spans(
                ctx.novel.id, state.envelope.chapter_index, state.display_spans,
                state.term_renderings,
            )
        log.info("stage %s chapter=%d linked=%d unlinked=%d", self.name,
                 state.envelope.chapter_index,
                 sum(bool(span.alias_id) for span in state.display_spans),
                 sum(not span.alias_id for span in state.display_spans))

    async def _linked_mentions(self, ctx: StageContext, state: PipelineState) -> None:
        if ctx.novel.source_lang == ctx.novel.target_lang:
            # Displayed text IS the source text, unchanged — the step-2 scan already
            # computed correct offsets against it. Re-scanning would be redundant work
            # producing an identical result.
            # ``mention_span`` is the legacy presentation ledger. Managed identity is
            # revision-scoped and is published through display_mention/mention_binding;
            # carrying those ids into this table would cross the revision fence (§0.3).
            if await self._legacy_presentation_revision(ctx) is not None:
                state.display_spans = state.mentions
            else:
                state.display_spans = [span.model_copy(update={"alias_id": ""})
                                       for span in state.mentions]
            return

        if state.translation is None:
            # TRANSLATE was skipped or hasn't run (shouldn't happen once source_lang !=
            # target_lang is pinned, but leave nothing to highlight rather than scan the
            # wrong text).
            return

        # The target term remains useful presentation data even while its source term
        # has no entity. Resolve ids only from the verified legacy binding for the exact
        # active revision. In particular, never use glossary.entity_id or a managed
        # revision's binding in the legacy mention_span table.
        legacy_revision = await self._legacy_presentation_revision(ctx)
        rows = await (
            await ctx.db.execute(
                """
                SELECT g.source_term, g.target_term, b.entity_id
                  FROM glossary g
                  LEFT JOIN glossary_binding b
                   ON b.novel_id = g.novel_id
                   AND b.source_term = g.source_term
                   AND b.revision_id = %s::uuid
                   AND g.locked_at_chapter <= %s
                   AND b.known_from_chapter <= %s
                 WHERE g.novel_id = %s AND NOT g.deleted
                """,
                (legacy_revision, state.envelope.chapter_index, state.envelope.chapter_index,
                 ctx.novel.id),
            )
        ).fetchall()

        request = MentionScanRequest(
            text=state.translation,
            aliases=[
                Alias(alias_id=str(index), surface=target_term)
                for index, (_, target_term, _entity_id) in enumerate(rows)
            ],
            lang=ctx.novel.target_lang,
        )
        response = await ctx.textproc.scan(request) if ctx.textproc else scan_mentions(request)
        state.display_spans = []
        for span in response.spans:
            source_term, target_term, entity_id = rows[int(span.alias_id)]
            # Empty alias ids are intentional placeholders. GraphWriter stores them as
            # NULL, while term_renderings preserves the source/display ledger for later
            # managed revision materialization.
            state.display_spans.append(span.model_copy(update={
                "alias_id": str(entity_id) if entity_id is not None else "",
            }))
            state.term_renderings.append(TermRenderingOccurrence(
                source_term, target_term, span.char_start, span.char_end, "glossary"))

        log.debug(
            "stage %s chapter=%d terms=%d spans=%d",
            self.name,
            state.envelope.chapter_index,
            len(rows),
            len(state.display_spans),
        )

    async def _legacy_presentation_revision(self, ctx: StageContext) -> str | None:
        """Return the only revision allowed to supply legacy entity links.

        ``GraphWriter.replace_mention_spans`` intentionally has no revision argument and
        migration 0024's trigger assigns the legacy revision. Therefore a managed or
        quarantined graph may still write empty presentation spans, but it may not attach
        an id from that graph to them. This is a write-path fence; reader authorization
        remains the revision/RLS fence on managed display rows (§0.3).
        """
        row = await (
            await ctx.db.execute(
                """
                SELECT r.id
                  FROM novel n
                  JOIN graph_revision r ON r.id = n.active_graph_revision
                 WHERE n.id = %s
                   AND r.legacy AND r.state = 'active' AND r.trusted
                """,
                (ctx.novel.id,),
            )
        ).fetchone()
        return str(row[0]) if row else None
