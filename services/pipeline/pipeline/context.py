"""Stage contracts: ``StageContext`` (read-only, resolved once per chapter) and
``PipelineState`` (the mutable accumulator threaded through the stage list).

These are the shapes every stage inherits (from the CHUNK design). A stage reads what
it needs from ``ctx`` and writes its output onto ``state``; the narrowness of what a
stage touches is what keeps it testable and, for the pure stages, deterministic (§0.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pipeline.config import Config
from pipeline.envelope import ChapterEnvelope

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from minio import Minio

    from pipeline.llm.provider import LLMProvider


@dataclass(frozen=True)
class NovelMeta:
    """The per-novel facts resolved once and handed to every stage."""

    id: str
    source_lang: str
    target_lang: str
    ontology: dict[str, Any]


@dataclass(frozen=True)
class LanguageProfile:
    """Sibling to the ontology (§5.4), keyed by source_lang.

    Placeholder this phase — the CHUNK step (1.4) reduces it to the two fields the
    chunker needs (sentence terminators + a token-estimate divisor) and resolves it
    from ``source_lang``. Present now so ``StageContext`` has a stable shape.
    """

    lang: str


@dataclass(frozen=True)
class StageContext:
    """Read-only, resolved once per chapter by the worker. Stages must not mutate it."""

    novel: NovelMeta
    language_profile: LanguageProfile
    provider: "LLMProvider"
    db: "AsyncConnection"
    objects: "Minio"
    cfg: Config


@dataclass
class PipelineState:
    """Mutable accumulator. Each stage fills its slot; later stages read earlier ones."""

    envelope: ChapterEnvelope
    chunks: list[Any] = field(default_factory=list)  # chunk stage (1.4)
    mentions: list[Any] = field(default_factory=list)  # scan stage
    resolutions: list[Any] = field(default_factory=list)  # resolve stage
    translation: str | None = None  # translate stage
    extractions: list[Any] = field(default_factory=list)  # state stage
