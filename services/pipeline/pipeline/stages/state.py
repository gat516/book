"""Stage 5: STATE EXTRACT (instructions.md §5 step 5) — NO-OP STUB.

LLM pulls events + state changes for the ontology's tracked attributes/relations, tagged
with this chapter_index; batched. Filled in step 1.5 (the first real LLM stage) — it's
the stage that populates fact/edge/event, which every downstream feature reads.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


class StateStage:
    name = "state"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        log.debug("stage %s (stub) chapter=%d", self.name, state.envelope.chapter_index)
