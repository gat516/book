"""Pipeline stages (instructions.md §5). Runtime order: chunk → scan → resolve →
translate → display-scan → state → graph-write. Chunk and graph-write are real (1.4),
state is real (1.5), scan and resolve are real (1.6), translate is real (1.7),
display-scan is real (Phase 5.2).

The build order deliberately differs from the runtime order (PLAN.md): state was filled
before resolve even though it runs after it, because state populates fact/edge/event —
what every downstream feature reads — and can be developed against an English test novel
where resolution is easy and translation is skipped. Resolve then replaced the
exact-match placeholder that stood in for it, and now owns ``state.resolutions``, the
single surface → entity_id map every later stage binds through.

display-scan sits right after translate (produce the display text, then scan it) and
before state — state-extract's LLM call has no dependency on display spans either way,
so its position relative to state is free.
"""

from pipeline.stages.chunk import ChunkStage
from pipeline.stages.display_scan import DisplayScanStage
from pipeline.stages.graph_write import GraphWriteStage
from pipeline.stages.resolve import ResolveStage
from pipeline.stages.scan import ScanStage
from pipeline.stages.state import StateStage
from pipeline.stages.translate import TranslateStage

# The pipeline in runtime order (§5).
DEFAULT_STAGES = [
    ChunkStage(),
    ScanStage(),
    ResolveStage(),
    TranslateStage(),
    DisplayScanStage(),
    StateStage(),
    GraphWriteStage(),
]

__all__ = [
    "ChunkStage",
    "ScanStage",
    "ResolveStage",
    "TranslateStage",
    "DisplayScanStage",
    "StateStage",
    "GraphWriteStage",
    "DEFAULT_STAGES",
]
