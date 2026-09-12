"""Production stages: chunk → translate → records.

The build order deliberately differs from the runtime order (PLAN.md): state was filled
before resolve even though it runs after it, because state populates fact/edge/event —
what every downstream feature reads — and can be developed against an English test novel
where resolution is easy and translation is skipped. Resolve then replaced the
exact-match placeholder that stood in for it, and now owns ``state.resolutions``, the
single surface → entity_id map every later stage binds through.

Translation intentionally uses the glossary locked by *previously completed* work. New
terms discovered while enriching this chapter apply forward-only to later translations
(§0.2); knowledge-graph latency must never hold reader-visible prose hostage (§5).
"""

from pipeline.stages.character_names import CharacterNamesStage
from pipeline.stages.chunk import ChunkStage
from pipeline.stages.display_scan import DisplayScanStage
from pipeline.stages.records import RecordsStage
from pipeline.stages.scan import ScanStage
from pipeline.stages.translate import TranslateStage

# The pipeline in runtime order (§5).
DEFAULT_STAGES = [
    ChunkStage(),
    TranslateStage(),
    CharacterNamesStage(),
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
