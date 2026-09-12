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

    from pipeline.batch import BatchManager
    from pipeline.cache import LLMCache
    from pipeline.llm.provider import LLMProvider
    from pipeline.mentions import Span
    from pipeline.textproc import TextProcClient
    from pipeline.display_names import TermRenderingOccurrence


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
    batch_manager: "BatchManager"
    embed_provider: "LLMProvider"
    db: "AsyncConnection"
    objects: "Minio"
    cfg: Config
    cache: "LLMCache"  # the §6.1 LLM-result cache, shared by every LLM-bearing stage
    textproc: "TextProcClient | None" = None

    # The provider identity actually backing `provider`/`batch_manager` for this chapter
    # (PLAN.md Phase N4): the novel's own novel_provider_config.provider if it has one,
    # else cfg.llm_provider. "" (the default) means "not set — fall back to cfg.llm_provider"
    # for callers/tests built before this field existed; the worker always sets it for real.
    provider_id: str = ""

    # CHARACTER_NAMES-only provider, carrying that stage's own prefill/idle/total budget
    # (config.names_runtime). None means "no dedicated budget — use `provider`", which is
    # the case for every non-Ollama backend: the budget is expressed as OllamaProvider
    # constructor kwargs, and a hosted provider pinned by novel_provider_config must keep
    # its own routing rather than be silently replaced by a local Ollama client.
    names_provider: "LLMProvider | None" = None

    # RESOLVE-only Ollama provider. Its streaming transport separates slow-but-healthy
    # prompt evaluation from an actual inter-token stall; hosted providers leave this
    # unset and retain their normal per-novel provider routing (§5.4).
    resolve_provider: "LLMProvider | None" = None

    # Optional per-book stage models. The provider is shared, while translation can use
    # a stronger model and all graph/extraction stages use the extraction model.
    model_override: dict[str, str] | str | None = None


@dataclass
class PipelineState:
    """Mutable accumulator. Each stage fills its slot; later stages read earlier ones."""

    envelope: ChapterEnvelope
    chunks: list[Chunk] = field(default_factory=list)  # chunk stage (1.4)
    mentions: "list[Span]" = field(default_factory=list)  # scan stage (1.6)

    # display-scan stage (Phase 5.2): mentions against the DISPLAY text (translated, or
    # source if untranslated), for the reader UI's highlighting. Offsets here are NOT
    # comparable to `mentions` above when the novel is translated — different text.
    # An empty alias_id denotes a literal named mention with no entity binding; the
    # database stores it as NULL. Never use these spans to bind graph facts.
    display_spans: "list[Span]" = field(default_factory=list)
    # Exact source-term -> display-span alignments. Derived terminology metadata only;
    # it never binds an entity and is independently chapter-gated on reads (§0.3).
    term_renderings: "list[TermRenderingOccurrence]" = field(default_factory=list)

    # resolve stage (1.6): the AUTHORITATIVE occurrence-ID -> entity_id map (revision pipeline). Every stage that
    # needs to turn a name into an id reads this and nothing else — a surface absent from
    # it is an unresolved mention, not an invitation to bind by exact match (that was the
    # 1.5 placeholder, and exact matching is the entity-drift bug §12 risk #2 describes).
    resolutions: dict[str, str] = field(default_factory=dict)

    translation: str | None = None  # translate stage

    # Records enrichment output: parsed records, check results, the authoritative
    # who's-who resolution and renderings. ``None`` means the stage did not run, which
    # is NOT the same as a chapter that legitimately yielded nothing — the publisher
    # writes a completed empty run for the second and nothing at all for the first.
    records: dict[str, Any] | None = None
