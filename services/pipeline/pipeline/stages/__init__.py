"""Pipeline stages (instructions.md §5). Runtime order: chunk → scan → resolve →
translate → state → graph-write. Chunk and graph-write are real (1.4); scan/resolve/
translate/state remain no-op stubs until 1.5-1.7.
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
