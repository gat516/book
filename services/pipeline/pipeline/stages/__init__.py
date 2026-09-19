"""Runtime: chunk → translate → display scan → facts → chunk index.

Translation becomes readable independently of optional enrichment. Names are decided
from the source before translating and primed into it; display scan finds them by exact
search. FACTS replaced RECORDS (.claude/plans/facts-stage.md): one call per chapter
writes story facts, and wiki pages are built from them.
"""

from pipeline.stages.chunk import ChunkStage
from pipeline.stages.chunk_index import ChunkIndexStage
from pipeline.stages.display_scan import DisplayScanStage
from pipeline.stages.facts import FactsStage
from pipeline.stages.translate import TranslateStage

# The pipeline in runtime order (§5). Terminology choices reuse display alignment;
# the legacy standalone character-name inventory is not part of normal ingestion.
DEFAULT_STAGES = [
    ChunkStage(),
    TranslateStage(),
    # §0.5 / §0.7: name choices depend on readable prose, not optional extraction.
    # A facts failure must not prevent the reader from receiving corrected names.
    DisplayScanStage(),
    # FACTS has no cross-chapter dependency, so no ordering wait.
    FactsStage(),
    # askai's retrieval chunks.
    ChunkIndexStage(),
]

__all__ = [
    "ChunkIndexStage",
    "ChunkStage",
    "DisplayScanStage",
    "FactsStage",
    "TranslateStage",
    "DEFAULT_STAGES",
]
