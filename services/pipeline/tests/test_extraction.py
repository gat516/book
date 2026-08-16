"""The state stage's contract with the model (PLAN.md 1.5, instructions.md §4.1, §5, §6.2).

Two properties matter here and neither is about happy-path JSON:

- The prompt is **templated on the ontology** (§0.4). Nothing genre-specific may be
  baked into the code, so a different ontology must produce a visibly different prompt.
- The parse is a **validating boundary**. Model output is untrusted input; it either
  becomes a well-formed ``Extraction`` or it raises, never a half-populated object that
  writes junk into an append-only graph.

Pure — no DB, no network, no LLM.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pipeline.extraction import (
    build_system_prompt,
    build_user_prompt,
    parse_extraction,
)

XIANXIA = {
    "kinds": ["character", "sect", "technique"],
    "attributes": [
        {"name": "rank", "kinds": ["character"]},
        {"name": "state", "kinds": ["sect"]},
    ],
    "relations": ["member_of", "enemy"],
}

MYSTERY = {
    "kinds": ["character", "clue", "organization"],
    "attributes": [{"name": "alibi", "kinds": ["character"]}],
    "relations": ["suspects", "employs"],
}


# --- the prompt is data, not code -------------------------------------------


def test_prompt_carries_this_novels_ontology():
    prompt = build_system_prompt(XIANXIA)
    for term in ("character", "sect", "technique", "rank", "member_of", "enemy"):
        assert term in prompt


def test_different_ontology_gives_different_prompt():
    """The genre-agnosticism claim (§0.4) in one assertion: same code, different JSON,
    different extraction request — and no leakage of one genre's vocabulary into the
    other's prompt."""
    xianxia = build_system_prompt(XIANXIA)
    mystery = build_system_prompt(MYSTERY)
    assert xianxia != mystery
    assert "sect" not in mystery
    assert "alibi" not in xianxia


def test_attribute_kinds_are_shown_to_the_model():
    # An attribute is only meaningful against the kinds it applies to; a prompt that
    # lists bare attribute names invites "rank" facts about locations.
    assert "rank (character)" in build_system_prompt(XIANXIA)


def test_empty_ontology_does_not_produce_a_broken_prompt():
    """An auto-induced ontology (§4.2) can arrive thin or empty. That should ask for
    nothing, not crash the stage or emit a dangling header."""
    prompt = build_system_prompt({})
    assert "none declared" in prompt


def test_string_attributes_are_tolerated():
    # §4.1's shape is objects, but an LLM-authored ontology (§4.2) may emit bare strings
    # and the graph never validates against the preset anyway.
    assert "- alibi (any kind)" in build_system_prompt({"attributes": ["alibi"]})


def test_chapter_text_goes_after_the_stable_prefix():
    """§6.2's cost footgun: the system block is the cacheable prefix, so chapter text
    must live in the user block. If this ever inverts, prefix caching silently stops
    firing and no error is raised — only the bill moves."""
    body = "他走进了青云宗的大门。"
    assert body in build_user_prompt(body)
    assert body not in build_system_prompt(XIANXIA)


# --- the parse is a boundary -------------------------------------------------


def _payload(**overrides) -> str:
    base = {"entities": [], "facts": [], "edges": [], "events": []}
    base.update(overrides)
    return json.dumps(base)


def test_parses_a_full_extraction():
    extraction = parse_extraction(
        _payload(
            entities=[{"surface": "李逍遥", "kind": "character"}],
            facts=[
                {
                    "entity": "李逍遥",
                    "attribute": "rank",
                    "value": "Foundation Establishment",
                    "valid_from_chapter": None,
                    "confidence": 0.9,
                }
            ],
            edges=[{"src": "李逍遥", "dst": "青云宗", "rel_type": "member_of"}],
            events=[{"summary": "He breaks through.", "entities": ["李逍遥"]}],
        )
    )
    assert extraction.entities[0].kind == "character"
    assert extraction.facts[0].valid_from_chapter is None
    assert extraction.edges[0].rel_type == "member_of"
    assert extraction.events[0].entities == ["李逍遥"]


def test_empty_extraction_is_valid():
    """A chapter of pure scenery yields nothing. That must parse cleanly — treating it
    as a failure would retry, re-pay, and get the same empty answer."""
    assert parse_extraction("{}").facts == []


def test_markdown_fence_is_tolerated():
    # The single most common deviation from "return JSON and nothing else". Absorbing it
    # here is cheaper than losing a chapter's extraction to three backticks.
    fenced = "```json\n" + _payload(facts=[]) + "\n```"
    assert parse_extraction(fenced).facts == []


def test_prose_response_raises():
    """Not-JSON is a stage failure (retryable), never a silent empty extraction — the
    latter would mark the chapter done and leave a permanent hole in the graph."""
    with pytest.raises(Exception):
        parse_extraction("I'm sorry, I can't help with that.")


def test_confidence_is_clamped():
    # Models emit 1.5 and -0.2 with a straight face; the column is a REAL with no check
    # constraint, so the clamp has to happen here or the tiebreak ordering (0005) skews.
    extraction = parse_extraction(
        _payload(
            entities=[{"surface": "A", "kind": "character"}],
            facts=[{"entity": "A", "attribute": "status", "value": "alive", "confidence": 1.5}],
        )
    )
    assert extraction.facts[0].confidence == 1.0


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        parse_extraction(_payload(facts=[{"entity": "A", "attribute": "status"}]))
