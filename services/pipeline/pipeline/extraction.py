"""The STATE-EXTRACT contract: what we ask the LLM for, and what we accept back
(instructions.md §5 step 5, §4.1; PLAN.md 1.5).

Two things live here, deliberately apart from the stage that runs them:

- **The prompt, templated on ``novel.ontology``** (§0.4, §4.1). It asks for *this*
  novel's kinds, attributes and relations. Nothing about cultivation realms or noble
  houses is hardcoded anywhere; swap the ontology JSON and the same code extracts a
  different genre.
- **The pydantic models the response must validate against.** An LLM returning prose,
  a wrong-shaped object, or a hallucinated entity reference is an ordinary Tuesday, so
  the boundary between "model output" and "graph write" is a validating parse, never a
  ``json.loads`` and a hope.

Prompt layout is load-bearing for cost (§6.2): the system block holds everything stable
across a novel (instructions + ontology) and the user block holds only the chapter text.
That ordering is what lets provider prefix caching fire — DeepSeek automatically,
Anthropic via an explicit ``cache_control`` marker on the system block. Putting chapter
text first silently ~10x's the input bill and no error is raised.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# What we accept back
# ---------------------------------------------------------------------------


class ExtractedEntity(BaseModel):
    """An entity the chapter mentions. ``surface`` is the name as it appears in the
    source text; binding it to a real ``entity.id`` is resolution's job, not the
    extractor's (§5 "retrieve-then-resolve")."""

    surface: str
    kind: str


class ExtractedFact(BaseModel):
    """One attribute assertion about one entity.

    ``valid_from_chapter`` is STORY-time and optional — the extractor sets it only when
    the chapter says a state change happened earlier (a flashback: "he had been expelled
    years ago"). Left unset it defaults to this chapter. It is NEVER the gate key;
    ``source_chapter`` is, and the stage sets that itself from the chapter being
    processed — the model is not asked for it and could not be trusted with it (§0.2).
    """

    entity: str
    attribute: str
    value: str
    valid_from_chapter: int | None = None
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return min(1.0, max(0.0, v))


class ExtractedEdge(BaseModel):
    """A relation between two entities, from the ontology's ``relations`` list."""

    src: str
    dst: str
    rel_type: str
    valid_from_chapter: int | None = None


class ExtractedEvent(BaseModel):
    """A timeline row. ``entities`` are surfaces, resolved like everything else."""

    summary: str
    entities: list[str] = Field(default_factory=list)


class Extraction(BaseModel):
    """The whole response object. Every list defaults empty: a chapter of pure scenery
    legitimately yields nothing, and that must not look like a parse failure."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    edges: list[ExtractedEdge] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# What we ask for
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You extract structured knowledge from one chapter of a serialized novel.

You will be given the text of a single chapter. Report only what THIS chapter states or
directly implies. Do not use knowledge of the wider story, and do not speculate about
what happens later.

This novel tracks the following ontology.

Entity kinds:
{kinds}

Tracked attributes (attribute -> the kinds it applies to):
{attributes}

Relation types:
{relations}

Return a single JSON object, and nothing else, with this shape:

{{
  "entities": [{{"surface": "<name exactly as written in the chapter>", "kind": "<one of the kinds above>"}}],
  "facts":    [{{"entity": "<a surface listed in entities>", "attribute": "<one of the attributes above>",
                "value": "<short value>", "valid_from_chapter": <int or null>, "confidence": <0.0-1.0>}}],
  "edges":    [{{"src": "<a surface>", "dst": "<a surface>", "rel_type": "<one of the relations above>",
                "valid_from_chapter": <int or null>}}],
  "events":   [{{"summary": "<one sentence>", "entities": ["<a surface>"]}}]
}}

Rules:
- Every surface named in facts, edges or events MUST also appear in "entities". Entries
  referencing an undeclared surface are discarded.
- Use only the kinds, attributes and relation types listed above. If something important
  does not fit them, omit it rather than inventing a category.
- Set "valid_from_chapter" ONLY for a change the chapter says happened in an earlier
  chapter (a flashback or a revelation about the past). For anything happening now,
  leave it null.
- "confidence" is your own certainty: 1.0 for something stated outright, lower for
  something inferred.
- Emit an empty list rather than omitting a key. Return no prose, no markdown fence.
"""

_NO_ATTRIBUTES = "(none declared — emit an empty facts list)"
_NO_RELATIONS = "(none declared — emit an empty edges list)"
_NO_KINDS = "(none declared — emit an empty entities list)"


def format_kinds(ontology: dict[str, Any]) -> str:
    """Public because RESOLVE's proposal prompt (resolution.py) asks for the same kinds
    list. One formatter means the two prompts cannot drift into describing the same
    ontology differently."""
    kinds = [str(k) for k in ontology.get("kinds", [])]
    return "\n".join(f"- {k}" for k in kinds) if kinds else _NO_KINDS


def _format_attributes(ontology: dict[str, Any]) -> str:
    """Attributes are objects (``{"name": ..., "kinds": [...]}``) per §4.1, but tolerate a
    bare string list too — an auto-induced ontology (§4.2) is LLM-authored and the graph
    never validates against the preset anyway."""
    lines: list[str] = []
    for attr in ontology.get("attributes", []):
        if isinstance(attr, str):
            lines.append(f"- {attr} (any kind)")
            continue
        name = str(attr.get("name", "")).strip()
        if not name:
            continue
        kinds = [str(k) for k in attr.get("kinds", [])]
        lines.append(f"- {name} ({', '.join(kinds)})" if kinds else f"- {name} (any kind)")
    return "\n".join(lines) if lines else _NO_ATTRIBUTES


def _format_relations(ontology: dict[str, Any]) -> str:
    relations = [str(r) for r in ontology.get("relations", [])]
    return "\n".join(f"- {r}" for r in relations) if relations else _NO_RELATIONS


def build_system_prompt(ontology: dict[str, Any]) -> str:
    """The stable prefix: instructions + this novel's ontology, no chapter text (§6.2)."""
    return _SYSTEM_TEMPLATE.format(
        kinds=format_kinds(ontology),
        attributes=_format_attributes(ontology),
        relations=_format_relations(ontology),
    )


def build_user_prompt(raw_text: str) -> str:
    """The volatile suffix: only the chapter body, so the prefix above stays cacheable."""
    return f"<chapter>\n{raw_text}\n</chapter>"


def parse_extraction(text: str) -> Extraction:
    """Validating parse of a model response.

    Tolerates a markdown fence around the JSON — the single most common deviation from
    "return JSON and nothing else", and cheap to absorb here rather than losing a whole
    chapter's extraction to three backticks. Anything else raises, and the caller treats
    that as a stage failure (retryable) rather than writing junk to the graph.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return Extraction.model_validate(json.loads(stripped))
