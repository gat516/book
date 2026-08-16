"""The CHUNK stage (PLAN.md 1.4 Task 4, instructions.md §5 step 1). Pure — no DB, no
network — asserting the packing/offset contract graph_write.py's embed call depends on.
"""

from __future__ import annotations

from pipeline.context import language_profile_for
from pipeline.stages.chunk import chunk_text


def test_offsets_round_trip_against_raw_text():
    text = "First paragraph, one sentence.\n\nSecond paragraph. Two sentences here."
    profile = language_profile_for("en")
    chunks = chunk_text(text, profile)
    assert len(chunks) > 0
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_never_splits_mid_sentence():
    text = "Sentence one is short. " + ("Padding word " * 60) + "final sentence here."
    profile = language_profile_for("en")
    chunks = chunk_text(text, profile, budget=30)
    assert len(chunks) > 1
    for c in chunks:
        stripped = c.text.strip()
        assert stripped.endswith((".", "!", "?", "…")) or c is chunks[-1]


def test_budget_roughly_respected():
    profile = language_profile_for("en")
    sentence = "This is a padding sentence of fixed length for budget testing. "
    text = sentence * 100
    chunks = chunk_text(text, profile, budget=50)
    # every non-final chunk should be near the budget, not wildly over it
    for c in chunks[:-1]:
        estimated_tokens = len(c.text) / profile.chars_per_token
        assert estimated_tokens <= 50 + (len(sentence) / profile.chars_per_token)


def test_ordinals_are_sequential():
    text = "One. Two. Three. Four. Five."
    profile = language_profile_for("en")
    chunks = chunk_text(text, profile, budget=1)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_cjk_text_chunks_without_whitespace():
    # No spaces at all — a whitespace word-count would see this as a single "word".
    text = "这是第一句话。这是第二句话！\n\n这是第二段，包含更多汉字用于测试分段逻辑。"
    profile = language_profile_for("zh")
    chunks = chunk_text(text, profile, budget=10)
    assert len(chunks) >= 2
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_empty_text_produces_no_chunks():
    profile = language_profile_for("en")
    assert chunk_text("", profile) == []


def test_paragraph_boundary_preserved_in_offsets():
    text = "Para one sentence.\n\nPara two sentence."
    profile = language_profile_for("en")
    chunks = chunk_text(text, profile, budget=1000)
    # one big chunk since budget is generous; offsets still must be exact
    assert len(chunks) == 1
    assert chunks[0].text == text
