"""Final stage: GRAPH-WRITE (instructions.md §5 step 7; PLAN.md 1.4, 1.5) — the sink.

Two kinds of output land here, under the two write disciplines graph.py describes:
chunks + embeddings (derived data, delete-and-reinsert), and the state stage's extraction
(knowledge, append-only). Everything happens inside one per-chapter transaction, so a
crash mid-chapter leaves nothing partial — including the ``job`` row that says the
extraction was written, which is flipped to ``done`` in that same transaction. Marking it
outside would open a window where the facts exist and the job doesn't know it, and the
retry would insert them all again.

Surfaces become entity ids via ``state.resolutions``, which RESOLVE (1.6) owns and which
is the only binding path here. This stage does no name matching of its own — the
exact-match placeholder it used through 1.5 is gone, because exact matching is the
entity-drift bug (§12 risk #2), not a mild approximation of resolution.

``state.extraction is None`` means the state stage skipped itself (its work was already
written); that is deliberately distinct from an empty ``Extraction``, which means the
model looked and found nothing worth recording. The first must write nothing; the second
may legitimately write nothing but still marks its job done.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.extraction import Extraction
from pipeline.graph import EdgeRow, EventRow, FactRow, GraphWriter
from pipeline.jobs import mark_job_done

log = logging.getLogger(__name__)


def _story_time(declared: int | None, source_chapter: int, *, what: str) -> int:
    """Resolve a fact/edge's ``valid_from_chapter`` (story-time) against its
    ``source_chapter`` (knowledge-time).

    Unset means "it happened now" → the source chapter. A value in the future is the
    extractor hallucinating, and it gets clamped: story-time never drives the spoiler
    gate (§0.3 gates on ``source_chapter``), so this cannot leak anything — but an event
    that becomes true after the chapter that reports it is nonsense on the timeline and
    would render as such. Clamping keeps one bad integer from being the reason a whole
    chapter's extraction is thrown away.
    """
    if declared is None:
        return source_chapter
    if declared > source_chapter:
        log.warning(
            "%s claims story-time chapter %d after knowledge-time %d; clamping",
            what,
            declared,
            source_chapter,
        )
        return source_chapter
    return declared


class GraphWriteStage:
    name = "graph_write"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        writer = GraphWriter(ctx.db)
        await writer.ready()

        chapter_index = state.envelope.chapter_index
        texts = [c.text for c in state.chunks]
        embeddings = await ctx.embed_provider.embed(texts) if texts else []

        async with ctx.db.transaction():
            await writer.replace_chunks(ctx.novel.id, chapter_index, state.chunks, embeddings)
            await writer.replace_mention_spans(ctx.novel.id, chapter_index, state.display_spans,
                                               state.term_renderings)

            written = (0, 0, 0, 0)
            if state.extraction is not None:
                written = await self._write_extraction(
                    ctx, writer, state.extraction, chapter_index, state.resolutions
                )
                if state.state_job_key is not None:
                    await mark_job_done(
                        ctx.db,
                        novel_id=ctx.novel.id,
                        chapter_index=chapter_index,
                        stage="state",
                        key=state.state_job_key,
                    )

        log.info(
            "stage %s chapter=%d chunks=%d facts=%d edges=%d events=%d unresolved=%d",
            self.name,
            chapter_index,
            len(state.chunks),
            *written,
        )

    async def _write_extraction(
        self,
        ctx: StageContext,
        writer: GraphWriter,
        extraction: Extraction,
        chapter_index: int,
        bound: dict[str, str],
    ) -> tuple[int, int, int, int]:
        allowed_kinds=set(ctx.novel.ontology.get("kinds",[]))
        declared: dict[str,str] = {}
        conflicting=set()
        for entity in extraction.entities:
            if entity.kind not in allowed_kinds:
                log.warning("dropping declaration %r: invalid ontology kind %r",entity.surface,entity.kind)
                conflicting.add(entity.surface);continue
            if entity.surface in declared and declared[entity.surface]!=entity.kind:
                conflicting.add(entity.surface);declared.pop(entity.surface,None);continue
            declared[entity.surface]=entity.kind
        invalid=0

        def resolved(*surfaces: str) -> bool:
            nonlocal invalid
            missing=[s for s in surfaces if s in conflicting or s not in declared or s not in bound]
            if missing:
                invalid+=1;log.warning("dropping extraction row: undeclared or unresolved %s",missing)
            return not missing

        attributes={}
        for attr in ctx.novel.ontology.get("attributes",[]):
            if isinstance(attr,str):
                attributes[attr]=allowed_kinds
            elif attr.get("name"):
                attributes[attr["name"]]=set(attr.get("kinds") or allowed_kinds)
        facts=[]
        for fact in extraction.facts:
            if (not resolved(fact.entity) or fact.attribute not in attributes
                or declared.get(fact.entity) not in attributes.get(fact.attribute,set())):
                if fact.entity in declared and fact.entity in bound:
                    invalid+=1;log.warning("dropping fact %r.%r: ontology-invalid attribute",fact.entity,fact.attribute)
                continue
            facts.append(fact)
        relations=set(ctx.novel.ontology.get("relations",[]))
        edges=[]
        for edge in extraction.edges:
            if not resolved(edge.src,edge.dst) or edge.rel_type not in relations:
                if edge.src in declared and edge.dst in declared and edge.src in bound and edge.dst in bound:
                    invalid+=1;log.warning("dropping edge %r -> %r: invalid relation %r",edge.src,edge.dst,edge.rel_type)
                continue
            edges.append(edge)
        events=[]
        for event in extraction.events:
            if not resolved(*event.entities):
                continue
            events.append((event,event.entities))

        fact_rows = [
            FactRow(
                novel_id=ctx.novel.id,
                entity_id=bound[f.entity],
                attribute=f.attribute,
                value=f.value,
                # source_chapter is knowledge-time and is set HERE from the chapter being
                # processed — never taken from the model, which has no way to know it and
                # every opportunity to get it wrong (§0.2, §0.3).
                source_chapter=chapter_index,
                valid_from_chapter=_story_time(
                    f.valid_from_chapter, chapter_index, what=f"fact {f.entity}.{f.attribute}"
                ),
                confidence=f.confidence,
            )
            for f in facts
        ]
        edge_rows = [
            EdgeRow(
                novel_id=ctx.novel.id,
                src_id=bound[e.src],
                dst_id=bound[e.dst],
                rel_type=e.rel_type,
                source_chapter=chapter_index,
                valid_from_chapter=_story_time(
                    e.valid_from_chapter, chapter_index, what=f"edge {e.src}->{e.dst}"
                ),
            )
            for e in edges
        ]
        event_rows = [
            EventRow(
                novel_id=ctx.novel.id,
                chapter_index=chapter_index,
                summary=ev.summary,
                entity_ids=[bound[s] for s in surfaces],
            )
            for ev, surfaces in events
        ]

        await writer.insert_facts(fact_rows)
        await writer.insert_edges(edge_rows)
        await writer.insert_events(event_rows)
        return len(fact_rows), len(edge_rows), len(event_rows), invalid
