"""Stage 3: RESOLVE (instructions.md §5 step 3) — NO-OP STUB.

Retrieve-then-resolve: for each mention, pull candidate entities (vector sim + exact
alias) and have the LLM confirm/disambiguate against candidates only — never
free-generate. Filled in step 1.6. Deliberately NOT content-cached: its output depends
on the live alias index (DB state), not just chapter text.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


class ResolveStage:
    name = "resolve"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        log.debug("stage %s (stub) chapter=%d", self.name, state.envelope.chapter_index)
