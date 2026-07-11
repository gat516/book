"""Stage 4: TRANSLATE (instructions.md §5 step 4) — NO-OP STUB.

Runs ONLY when source_lang != target_lang. LLM with the glossary injected as hard
constraints; batched. Filled in step 1.7. Skipped entirely for same-language novels.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


class TranslateStage:
    name = "translate"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        log.debug("stage %s (stub) chapter=%d", self.name, state.envelope.chapter_index)
