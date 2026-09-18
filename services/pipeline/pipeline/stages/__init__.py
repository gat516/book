"""Runtime: chunk → translate → scan → display scan → records.

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
    # §0.5 / §0.7: name choices depend on readable prose, not optional extraction.
    # A records failure must not prevent the reader from receiving corrected names.
    DisplayScanStage(),
    RecordsStage(),
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
