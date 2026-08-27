"""Prompt construction and validation for glossary-constrained translation."""

from __future__ import annotations

from typing import Any


class GlossaryViolation(ValueError):
    """A translation failed one or more locked terminology constraints."""


_SYSTEM = """\
Translate a serialized chapter from {source_lang} to {target_lang}.

The ontology below describes names whose identity must remain stable. The glossary is a
set of LOCKED TERM CONSTRAINTS: whenever a source term occurs, render its exact target
term, preserving capitalization and spelling. Never translate, paraphrase, inflect, or
replace a locked target term. Preserve paragraph breaks and return only the translation.

Ontology:
{ontology}

Locked glossary:
{glossary}
"""


def build_system_prompt(
    *, source_lang: str, target_lang: str, ontology: dict[str, Any], glossary: list[tuple[str, str]]
) -> str:
    import json

    listed = "\n".join(f"- {source} => {target}" for source, target in glossary)
    return _SYSTEM.format(
        source_lang=source_lang,
        target_lang=target_lang,
        ontology=json.dumps(ontology, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        glossary=listed or "(none yet)",
    )


def build_user_prompt(raw_text: str) -> str:
    return f"<chapter>\n{raw_text}\n</chapter>"


def validate_glossary_constraints(
    source_text: str,
    translated_text: str,
    glossary: list[tuple[str, str]],
) -> None:
    """Reject a completion that silently ignores a locked term used by this chapter."""
    if not translated_text.strip():
        raise GlossaryViolation("translation is empty")

    missing: list[str] = []
    untranslated: list[str] = []
    for source, target in glossary:
        # A blank source term makes this check nonsensical rather than strict:
        # "text".count("") is len(text) + 1, so an empty locked term would demand its
        # target appear ~2000 times in a chapter and fail EVERY translation of this novel
        # forever, under any model. Observed for real before resolve.py stopped locking
        # them (see _lock_glossary's guard) — this stays as the defensive half, since the
        # glossary is also writable by hand through the correction/bootstrap endpoints.
        if not source.strip() or not target.strip():
            continue
        required_count = source_text.count(source)
        if required_count == 0:
            continue
        actual_count = translated_text.count(target)
        if actual_count < required_count:
            missing.append(
                f"{source!r} => {target!r} ({actual_count}/{required_count})"
            )
        if source not in target and source in translated_text:
            untranslated.append(source)

    if missing or untranslated:
        details = []
        if missing:
            details.append("missing targets: " + ", ".join(missing))
        if untranslated:
            details.append("untranslated sources: " + ", ".join(repr(s) for s in untranslated))
        raise GlossaryViolation("; ".join(details))
