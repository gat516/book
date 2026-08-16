"""Prompt construction and validation for glossary-constrained translation."""

from __future__ import annotations

from typing import Any


_SYSTEM = """\
Translate a serialized novel chapter from {source_lang} to {target_lang}.

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

