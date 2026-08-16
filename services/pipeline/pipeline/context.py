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

    from pipeline.cache import LLMCache
    from pipeline.extraction import Extraction
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

    The two fields the chunker (1.4) needs: sentence terminators to split on, and a
    divisor for estimating token count from character count. Whitespace word-counting
    is meaningless for CJK source text (no spaces), so estimation is chars/divisor for
    every language rather than branching chunker logic on script.
    """

    lang: str
    sentence_terminators: str
    chars_per_token: float


_LANGUAGE_PROFILES: dict[str, LanguageProfile] = {
    "zh": LanguageProfile(lang="zh", sentence_terminators="。！？…", chars_per_token=1.5),
    "ja": LanguageProfile(lang="ja", sentence_terminators="。！？…", chars_per_token=1.5),
    "ko": LanguageProfile(lang="ko", sentence_terminators=".!?…", chars_per_token=2.0),
    "en": LanguageProfile(lang="en", sentence_terminators=".!?…", chars_per_token=4.0),
}
_DEFAULT_LANGUAGE_PROFILE = LanguageProfile(
    lang="", sentence_terminators=".!?…", chars_per_token=4.0
)


def language_profile_for(source_lang: str) -> LanguageProfile:
    """Resolve a ``LanguageProfile`` from ``novel.source_lang``, falling back to a
    generic Latin-script profile for languages without a tuned entry yet."""
    profile = _LANGUAGE_PROFILES.get(source_lang, _DEFAULT_LANGUAGE_PROFILE)
    return profile if profile.lang else LanguageProfile(
        lang=source_lang,
        sentence_terminators=profile.sentence_terminators,
        chars_per_token=profile.chars_per_token,
    )


@dataclass(frozen=True)
class Chunk:
    """One RAG chunk (spec §5 step 1). Offsets are against the SOURCE text — display
    spans over translated text are a separate second pass in step 6 (1.7), per §5."""

    ordinal: int
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class StageContext:
    """Read-only, resolved once per chapter by the worker. Stages must not mutate it."""

    novel: NovelMeta
    language_profile: LanguageProfile
    provider: "LLMProvider"
    embed_provider: "LLMProvider"
    db: "AsyncConnection"
    objects: "Minio"
    cfg: Config
    cache: "LLMCache"  # the §6.1 LLM-result cache, shared by every LLM-bearing stage


@dataclass
class PipelineState:
    """Mutable accumulator. Each stage fills its slot; later stages read earlier ones."""

    envelope: ChapterEnvelope
    chunks: list[Chunk] = field(default_factory=list)  # chunk stage (1.4)
    mentions: list[Any] = field(default_factory=list)  # scan stage
    resolutions: list[Any] = field(default_factory=list)  # resolve stage
    translation: str | None = None  # translate stage

    # state stage (1.5). ``extraction`` is None when the stage was skipped entirely
    # because its job row is already ``done`` — which is NOT the same as an empty
    # Extraction (a chapter that legitimately yielded nothing). graph-write must write
    # nothing in the first case and may write nothing in the second; conflating them
    # would re-insert an already-written chapter's facts (§0.2 is append-only).
    extraction: "Extraction | None" = None
    state_job_key: str | None = None
