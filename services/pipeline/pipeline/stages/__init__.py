"""Pipeline stages (instructions.md §5). Runtime order: chunk → scan → resolve →
translate → state → (graph-write). All are no-op stubs this phase; the worker runs the
full list so the skeleton is exercised end-to-end after every step.
"""

from pipeline.stages.chunk import ChunkStage
from pipeline.stages.resolve import ResolveStage
from pipeline.stages.scan import ScanStage
from pipeline.stages.state import StateStage
from pipeline.stages.translate import TranslateStage

# The pipeline in runtime order (§5). graph-write is added at step 1.4 (the sink).
DEFAULT_STAGES = [
    ChunkStage(),
    ScanStage(),
    ResolveStage(),
    TranslateStage(),
    StateStage(),
]

__all__ = [
    "ChunkStage",
    "ScanStage",
    "ResolveStage",
    "TranslateStage",
    "StateStage",
    "DEFAULT_STAGES",
]
