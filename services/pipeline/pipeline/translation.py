"""Prompt construction and validation for glossary-constrained translation."""

from __future__ import annotations

import html
import re
from typing import Any


class GlossaryViolation(ValueError):
    """A translation failed one or more locked terminology constraints."""

    def __init__(
        self,
        message: str,
        *,
        missing_targets: tuple[str, ...] = (),
        untranslated_sources: tuple[str, ...] = (),
        recoverable: bool = True,
        hard: bool = False,
    ) -> None:
        super().__init__(message)
        self.missing_targets = missing_targets
        self.untranslated_sources = untranslated_sources
        self.recoverable = recoverable
        self.hard = hard

    @property
    def term_count(self) -> int:
        return len(set(self.missing_targets) | set(self.untranslated_sources))


_SYSTEM = """\
Translate a serialized chapter from {source_lang} to {target_lang}.

The ontology below describes names whose identity must remain stable. The glossary is a
set of LOCKED TERM CONSTRAINTS, and every locked term has ALREADY been replaced with its
final {target_lang} form in the chapter below. Copy those forms through
character-for-character. If a locked source term still appears, render it as its listed
target. Never translate, paraphrase, inflect, or replace a locked target term.
Preserve paragraph breaks and return only the translation.

Ontology:
{ontology}

Locked glossary:
{glossary}
"""


def _term_rows(glossary):
    """Yield source, target, class while accepting legacy two-column test callers."""
    for row in glossary:
        yield row[0], row[1], row[2] if len(row) > 2 else "semantic_term"


def build_system_prompt(
    *, source_lang: str, target_lang: str, ontology: dict[str, Any], glossary
) -> str:
    import json

    listed = "\n".join(
        f"- {source} => {target}" + (" [CHARACTER NAME: exact spelling required]" if kind == "character_name" else "")
        for source, target, kind in _term_rows(glossary)
    )
    return _SYSTEM.format(
        source_lang=source_lang,
        target_lang=target_lang,
        ontology=json.dumps(ontology, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        glossary=listed or "(none yet)",
    )


def build_user_prompt(raw_text: str) -> str:
    return f"<chapter>\n{raw_text}\n</chapter>"


def prime_glossary_terms(source_text: str, glossary) -> str:
    """Substitute every locked source term with its locked target *before* the model runs.

    Terminology stability is structural here, not prompted — the same discipline §0 applies
    to spoilers ("authorization, not prompting") and that RESOLVE's ``_decide`` already
    applies when it overrides a model-proposed ``target_term`` rather than trusting a hint.

    The model's job collapses from "recall this mapping and apply it consistently" to
    "carry this phrase through", which small models do reliably and the previous
    prompt-only approach did not: a 3B model asked to render 九神殿 as its locked target
    produced it 0 times out of 3 required, on every retry, at every temperature.

    Doing this on the way *in* is what makes it tractable. Correcting the output afterwards
    would first require finding the model's own rendering ("Nine Gods Hall", "Ninth Divine
    Temple", or a pronoun) — an alignment problem as unreliable as the thing it fixes.

    Substituting the target rather than an opaque sentinel (the usual placeholder-protection
    trick) keeps a meaningful noun phrase in view, so the model still gets the surrounding
    grammar and articles right, and small models do not mangle it the way they mangle
    bracket glyphs.
    """
    usable = [(source, target) for source, target, _ in _term_rows(glossary) if source.strip() and target.strip()]
    if not usable:
        return source_text
    # Longest source first: a novel holding both 九神殿 and 九神殿主 must not have the
    # shorter term consume the prefix of the longer one. Python's alternation is
    # leftmost-then-first-listed, so ordering the pattern is what expresses "longest wins".
    usable.sort(key=lambda pair: len(pair[0]), reverse=True)
    replacement = dict(usable)
    pattern = re.compile("|".join(re.escape(source) for source, _ in usable))
    # One pass, so an injected target is never rescanned and cannot be substituted again
    # by a shorter term that happens to appear inside it.
    return pattern.sub(lambda match: replacement[match.group(0)], source_text)


_LOCKED_TAG = re.compile(r'<locked-term data-id="t\d+">(.*?)</locked-term>', re.DOTALL)


def protect_glossary_terms(source_text: str, glossary) -> str:
    """Second-attempt input that makes every locked target an explicit copy span."""
    usable = [(source, target) for source, target, _ in _term_rows(glossary) if source.strip() and target.strip()]
    usable.sort(key=lambda pair: len(pair[0]), reverse=True)
    if not usable:
        return source_text
    indexed = {source: (index, target) for index, (source, target) in enumerate(usable)}
    pattern = re.compile("|".join(re.escape(source) for source, _ in usable))

    def replace(match: re.Match[str]) -> str:
        index, target = indexed[match.group(0)]
        return f'<locked-term data-id="t{index}">{html.escape(target)}</locked-term>'

    return pattern.sub(replace, source_text)


def strip_locked_term_tags(text: str) -> str:
    """Remove only intact tags; malformed model output is left for validation to reject."""
    stripped=_LOCKED_TAG.sub(lambda match: html.unescape(match.group(1)), text)
    if "<locked-term" in stripped or "</locked-term>" in stripped:
        raise GlossaryViolation("protected translation contains malformed locked-term markup",recoverable=False)
    return stripped


def validate_glossary_constraints(
    source_text: str,
    translated_text: str,
    glossary,
) -> None:
    """Reject a completion that silently ignores a locked term used by this chapter.

    At-least-once, deliberately not occurrence parity. Parity is linguistically wrong:
    Chinese repeats a proper noun where English uses a pronoun ("it", "there", "the hall"),
    so a *good* translation legitimately renders the term fewer times than the source does.

    This is a backstop, no longer the mechanism. ``prime_glossary_terms`` establishes
    compliance before the model sees the text; what survives here is the case worth
    catching — a model that retranslates a primed term anyway, or drops it entirely.
    """
    if not translated_text.strip():
        raise GlossaryViolation("translation is empty", recoverable=False)

    missing: list[str] = []
    untranslated: list[str] = []
    hard_violation = False
    for source, target, kind in _term_rows(glossary):
        # A blank source term makes this check nonsensical rather than strict:
        # "text".count("") is len(text) + 1, so an empty locked term would demand its
        # target appear ~2000 times in a chapter and fail EVERY translation of this novel
        # forever, under any model. Observed for real before resolve.py stopped locking
        # them (see _lock_glossary's guard) — this stays as the defensive half, since the
        # glossary is also writable by hand through the correction/bootstrap endpoints.
        if not source.strip() or not target.strip():
            continue
        if source not in source_text:
            continue
        if target not in translated_text:
            missing.append(target)
            hard_violation = hard_violation or kind == "character_name"
        if source not in target and source in translated_text:
            untranslated.append(source)
            hard_violation = hard_violation or kind == "character_name"

    if missing or untranslated:
        details = []
        if missing:
            details.append("missing targets: " + ", ".join(repr(t) for t in missing))
        if untranslated:
            details.append("untranslated sources: " + ", ".join(repr(s) for s in untranslated))
        raise GlossaryViolation(
            "; ".join(details),
            missing_targets=tuple(missing),
            untranslated_sources=tuple(untranslated),
            hard=hard_violation,
        )
