"""Stage 2: MENTION SCAN (instructions.md §5 step 2) — NO-OP STUB.

Becomes a Python alias-matcher shaped like ``proto/textproc.proto`` in step 1.6, then a
gRPC client to the Rust ``textproc`` service in Phase 4.
"""

from __future__ import annotations

import logging

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


class ScanStage:
    name = "scan"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        log.debug("stage %s (stub) chapter=%d", self.name, state.envelope.chapter_index)
