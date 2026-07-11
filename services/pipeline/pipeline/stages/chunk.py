"""Stage 1: CHUNK (instructions.md §5 step 1) — NO-OP STUB.

Real work (token-budget splitting on paragraph/sentence boundaries, byte offsets into
raw_text) lands in PLAN.md step 1.4; the design is already drafted. For now it passes
through so the skeleton runs.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


class ChunkStage:
    name = "chunk"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        log.debug("stage %s (stub) chapter=%d", self.name, state.envelope.chapter_index)
