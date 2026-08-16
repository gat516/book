"""The RESOLVE contract: what we ask the LLM, and what we accept back
(instructions.md §5 "Entity resolution", §4.1, §12 risk #2; PLAN.md 1.6).

Same shape as ``extraction.py`` — pydantic models, ontology-templated prompt builders, a
validating parse — because that is now the established pattern for an LLM boundary here.

Resolution happens in two asks, and they are deliberately different in kind:

- **Proposal** is generative. It reads the chapter and names the surfaces that look like
  entities. This is *discovery*, and it exists because Aho-Corasick can only find aliases
  the graph already knows: a character introduced in chapter 41 has no alias, so the
  scanner is blind to them.
- **Disambiguation** is not generative about identity. Given one surface and a retrieved
  candidate list, the model may only confirm one candidate or declare the surface new.
  For a genuinely new entity in a translated novel it also proposes the target-language
  glossary term; same-language identity remains the exact source surface.

That second constraint is the fix for the highest-impact silent failure in the system
(§12 risk #2). A model free-generating a canonical name per chapter drifts — "Azure Cloud
Sect", "Blue Cloud Sect" and "Qingyun Sect" become three entities by chapter 600, nothing
errors, and every downstream feature is quietly wrong.

**So the constraint is enforced by the parser, not by the prompt.** ``parse_decision``
takes the candidate ids that were actually offered and rejects anything else. A prompt
that merely *asks* for discipline is a suggestion the model is free to decline on the one
chapter nobody reviews; a parse error is not.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from pipeline.extraction import format_kinds

NEW_ENTITY = "new"


# ---------------------------------------------------------------------------
# What we accept back
# ---------------------------------------------------------------------------


class ProposedMention(BaseModel):
    """A surface the model thinks names an entity, with an ontology kind."""

    surface: str
    kind: str


class Proposal(BaseModel):
    mentions: list[ProposedMention] = Field(default_factory=list)


class Candidate(BaseModel):
    """An existing entity offered to the disambiguator. ``entity_id`` is what comes back."""

    entity_id: str
    canonical: str
    kind: str


class Decision(BaseModel):
    """Confirm an offered candidate, or declare the surface a new entity.

    ``entity_id`` is set only when ``decision == "confirm"``, and ``parse_decision``
    has already checked it against the ids that were actually offered.
    """

    decision: str  # "confirm" | "new"
    entity_id: str | None = None
    target_term: str | None = None


class FreeGeneratedEntity(ValueError):
    """The disambiguator returned an entity id that was never offered to it.

    Its own error type because it is the §12 risk #2 tripwire, not a generic bad-JSON
    problem: it means the model is inventing identity rather than choosing it, and the
    caller should treat the mention as unresolved instead of binding to a fabricated id.
    """


# ---------------------------------------------------------------------------
# What we ask for
# ---------------------------------------------------------------------------

_PROPOSAL_SYSTEM = """\
You find the names of entities in one chapter of a serialized novel.

You will be given the text of a single chapter. List every distinct name it uses for an
entity of one of the kinds below — people, groups, places and so on as that list defines
them. Report only what THIS chapter contains; do not use knowledge of the wider story.

Entity kinds:
{kinds}

Return a single JSON object, and nothing else:

{{"mentions": [{{"surface": "<the name exactly as written in the chapter>", "kind": "<one of the kinds above>"}}]}}

Rules:
- "surface" must appear verbatim in the chapter. Do not translate, normalize, expand or
  correct it. If the chapter says "the old man", that is the surface.
- List each distinct surface once, even if it appears many times.
- Different names for what may be the same entity are DIFFERENT surfaces — list them all
  separately. Deciding whether two names mean one entity is not your task here.
- Use only the kinds listed above. Omit anything that fits none of them.
- Emit an empty list rather than omitting the key. No prose, no markdown fence.
"""

_DISAMBIGUATION_SYSTEM = """\
You decide which known entity a name in a novel refers to.

You will be given one name as it appeared in a chapter, the sentence around it, and a
list of entities already known in this novel. Decide whether the name refers to one of
those known entities, or to an entity not yet in the list.

Return a single JSON object, and nothing else:

{{"decision": "confirm", "entity_id": "<an entity_id from the candidate list>"}}
  or
{{"decision": "new"}}

Rules:
- "entity_id" MUST be copied exactly from the candidate list. Never invent an id, and
  never return an id that is not listed.
- Choose "new" only if the name refers to an entity that is genuinely not among the
  candidates. An alternate name, epithet, title or abbreviation for a candidate is a
  "confirm" of that candidate, not a new entity.
- If the candidate list is empty, the answer is "new".
- No prose, no markdown fence.
"""

_TRANSLATED_TERM_RULE = """

This novel is translated from {source_lang} to {target_lang}. When the decision is
"new", also return "target_term": a concise canonical name in {target_lang}. It becomes
a locked glossary term, so do not leave it blank and do not return the source-language
surface unless that spelling is intentionally unchanged in {target_lang}.
"""


def build_proposal_system_prompt(ontology: dict[str, Any]) -> str:
    """Stable prefix for the proposal pass: instructions + this novel's kinds (§6.2)."""
    return _PROPOSAL_SYSTEM.format(kinds=format_kinds(ontology))


def build_proposal_user_prompt(raw_text: str) -> str:
    """Volatile suffix: only the chapter body, so the prefix above stays cacheable."""
    return f"<chapter>\n{raw_text}\n</chapter>"


def build_disambiguation_system_prompt(*, source_lang: str, target_lang: str) -> str:
    """Stable prefix for disambiguation, specialized only by the language pair.

    Note it is NOT templated on the ontology: this ask is "which of these", and the
    candidates carry their own kinds. Keeping the ontology out means one identical prefix
    for every mention in a novel/language pair, which is what makes provider prefix
    caching useful on a pass that runs once per surface (§6.2).
    """
    if source_lang == target_lang:
        return _DISAMBIGUATION_SYSTEM
    return _DISAMBIGUATION_SYSTEM + _TRANSLATED_TERM_RULE.format(
        source_lang=source_lang, target_lang=target_lang
    )


def build_disambiguation_user_prompt(
    surface: str, candidates: list[Candidate], *, context: str = ""
) -> str:
    """Volatile suffix: the surface, its surrounding text, and the candidate list."""
    if candidates:
        listed = "\n".join(
            f'- entity_id: {c.entity_id} | canonical: {c.canonical} | kind: {c.kind}'
            for c in candidates
        )
    else:
        listed = "(no known entities match — the answer is \"new\")"
    parts = [f"Name: {surface}"]
    if context:
        parts.append(f"Context: {context}")
    parts.append(f"Candidates:\n{listed}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _strip_fence(text: str) -> str:
    """Tolerate a markdown fence — the commonest deviation from "JSON and nothing else"."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped


def parse_proposal(text: str) -> Proposal:
    return Proposal.model_validate(json.loads(_strip_fence(text)))


def parse_decision(text: str, offered: list[Candidate]) -> Decision:
    """Validating parse of a disambiguation response.

    The ``offered`` check is the load-bearing part and the reason this is not a bare
    ``model_validate``: an ``entity_id`` the model was never shown means it fabricated
    identity, which is precisely the drift §12 risk #2 describes. Raising
    ``FreeGeneratedEntity`` turns that into a visible, countable failure instead of a
    plausible-looking bind to an id that may not even exist.
    """
    decision = Decision.model_validate(json.loads(_strip_fence(text)))

    if decision.decision == NEW_ENTITY:
        return Decision(
            decision=NEW_ENTITY, entity_id=None, target_term=decision.target_term
        )

    if decision.decision != "confirm":
        raise ValueError(f"decision must be 'confirm' or 'new', got {decision.decision!r}")

    allowed = {c.entity_id for c in offered}
    if decision.entity_id not in allowed:
        raise FreeGeneratedEntity(
            f"disambiguator returned entity_id {decision.entity_id!r}, "
            f"which was not among the {len(allowed)} candidates offered (§12 risk #2)"
        )
    return decision
