"""Pipeline stages (instructions.md §5). Runtime order: chunk → scan → resolve →
translate → state → graph-write. Chunk and graph-write are real (1.4), state is real
(1.5); scan/resolve/translate remain no-op stubs until 1.6-1.7.

State runs before graph-write but is built before resolve, which runs before it (PLAN.md
1.5): it populates fact/edge/event, which every downstream feature reads, and it can be
developed against an English test novel where resolution is easy and translation skipped.
Until resolve lands, graph-write binds entity surfaces with a placeholder — see
``GraphWriter.bind_surfaces``.
"""

from pipeline.stages.chunk import ChunkStage
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
    StateStage(),
    GraphWriteStage(),
]

__all__ = [
    "ChunkStage",
    "ScanStage",
    "ResolveStage",
    "TranslateStage",
    "StateStage",
    "GraphWriteStage",
    "DEFAULT_STAGES",
]
