"""Runtime: chunk → translate → scan → records → display scan.

Translation becomes readable independently of optional records enrichment. Display
alignment supplies one provisional terminology choice for hovercard review and future
translations. Only RECORDS who's-who decisions authorize entity bindings (§0).
"""

from pipeline.stages.character_names import CharacterNamesStage
from pipeline.stages.chunk import ChunkStage
from pipeline.stages.display_scan import DisplayScanStage
from pipeline.stages.records import RecordsStage
from pipeline.stages.scan import ScanStage
from pipeline.stages.translate import TranslateStage

# The pipeline in runtime order (§5). Terminology choices reuse display alignment;
# the legacy standalone character-name inventory is not part of normal ingestion.
DEFAULT_STAGES = [
    ChunkStage(),
    TranslateStage(),
    ScanStage(),
    RecordsStage(),
    DisplayScanStage(),
]

__all__ = [
    "CharacterNamesStage",
    "ChunkStage",
    "DisplayScanStage",
    "RecordsStage",
    "ScanStage",
    "TranslateStage",
    "DEFAULT_STAGES",
]
