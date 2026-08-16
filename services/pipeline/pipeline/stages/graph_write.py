"""Final stage: GRAPH-WRITE (instructions.md §5; PLAN.md 1.4) — the sink.

This phase, only chunks flow through here: state/resolve (1.5/1.6) are still stubs, so
``state.extractions``/``state.resolutions`` are always empty and the entity/fact/edge/
event paths on ``GraphWriter`` go unused until those stages exist. Embeds
``state.chunks`` in one batched call (§5.4 — the embed backend takes the whole array,
not one call per chunk) and writes them inside a single per-chapter transaction.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext
from pipeline.graph import GraphWriter

log = logging.getLogger(__name__)


class GraphWriteStage:
    name = "graph_write"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        writer = GraphWriter(ctx.db)
        await writer.ready()

        texts = [c.text for c in state.chunks]
        embeddings = await ctx.embed_provider.embed(texts) if texts else []

        async with ctx.db.transaction():
            await writer.replace_chunks(
                ctx.novel.id, state.envelope.chapter_index, state.chunks, embeddings
            )

        log.debug(
            "stage %s chapter=%d chunks_written=%d",
            self.name,
            state.envelope.chapter_index,
            len(state.chunks),
        )
