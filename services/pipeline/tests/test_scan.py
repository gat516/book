"""Mention scanning (instructions.md §3.3, §5 step 2; PLAN.md 1.6).

This is the Python half of a contract the Rust ``textproc`` service will implement in
Phase 4, so the tests are written against the *contract* — leftmost-longest matching and
both offset units — rather than against pyahocorasick. When the transport swaps, these
assertions should still hold without edits; that is the whole reason the proto was
written before any Rust exists.

The dual-offset assertions are the load-bearing ones. On ASCII the two units coincide and
a bug is invisible; on CJK they diverge, and a byte offset used as a character offset
points into the middle of a codepoint. Since this system exists to process Chinese web
serials, the CJK case is the real one and the ASCII case is the accident.

Pure — no DB, no network.
"""

from __future__ import annotations

from pipeline.mentions import Alias, MentionScanRequest, scan_mentions


def _scan(text: str, aliases: dict[str, str], *, lang: str = "zh"):
    """``aliases`` is {alias_id: surface}."""
    return scan_mentions(
        MentionScanRequest(
            text=text,
            aliases=[Alias(alias_id=k, surface=v) for k, v in aliases.items()],
            lang=lang,
        )
    ).spans


def test_finds_a_simple_mention():
    [span] = _scan("Li Xiaoyao drew his sword.", {"e1": "Li Xiaoyao"})
    assert span.alias_id == "e1"
    assert (span.char_start, span.char_end) == (0, 10)


def test_offsets_slice_back_to_the_surface():
    """The property every consumer depends on and no consumer should have to re-derive."""
    text = "他到了青云宗的大门。"
    [span] = _scan(text, {"e1": "青云宗"})
    assert text[span.char_start : span.char_end] == "青云宗"
    assert text.encode("utf-8")[span.byte_start : span.byte_end] == "青云宗".encode("utf-8")


def test_leftmost_longest_prefers_the_longer_alias():
    """§3.3: prefer 王国 over 王. Plain Aho-Corasick would report both — the surname
    matched inside an unrelated word is the false positive the boundary filter exists
    for, and preferring the longest match is the cheap first line of defence."""
    spans = _scan("他统治着王国。", {"surname": "王", "kingdom": "王国"})
    assert [s.alias_id for s in spans] == ["kingdom"]


def test_leftmost_longest_applies_to_latin_text_too():
    spans = _scan("Li Xiaoyao arrived.", {"short": "Li", "full": "Li Xiaoyao"})
    assert [s.alias_id for s in spans] == ["full"]


def test_byte_and_char_offsets_coincide_on_ascii():
    [span] = _scan("Elder Chen waited.", {"e1": "Chen"}, lang="en")
    assert (span.char_start, span.char_end) == (span.byte_start, span.byte_end) == (6, 10)


def test_byte_and_char_offsets_diverge_on_cjk():
    """The bug the dual offsets exist to prevent, made explicit: three CJK characters
    occupy nine bytes, so the two units cannot be used interchangeably."""
    text = "第一章：青云宗"
    [span] = _scan(text, {"e1": "青云宗"})
    assert (span.char_start, span.char_end) == (4, 7)
    assert (span.byte_start, span.byte_end) == (12, 21)
    assert span.byte_end - span.byte_start == 3 * (span.char_end - span.char_start)


def test_offsets_are_correct_after_a_multibyte_prefix():
    """A regression guard for the mapping itself: a match *after* CJK text must have its
    byte offset shifted by the prefix's true byte length, not its character count."""
    text = "青云宗的 Li Xiaoyao"
    [span] = _scan(text, {"e1": "Li Xiaoyao"})
    assert text[span.char_start : span.char_end] == "Li Xiaoyao"
    assert span.byte_start == len("青云宗的 ".encode("utf-8"))


def test_every_occurrence_is_reported():
    spans = _scan("Chen met Chen.", {"e1": "Chen"}, lang="en")
    assert [(s.char_start, s.char_end) for s in spans] == [(0, 4), (9, 13)]


def test_shared_surface_yields_one_span_per_alias():
    """Two characters genuinely called "Chen". Collapsing them in the scanner would
    discard the candidate set before RESOLVE could weigh it — the ambiguity IS the
    signal, not noise to be cleaned up here."""
    spans = _scan("Chen bowed.", {"e1": "Chen", "e2": "Chen"}, lang="en")
    assert sorted(s.alias_id for s in spans) == ["e1", "e2"]
    assert {(s.char_start, s.char_end) for s in spans} == {(0, 4)}


def test_empty_alias_set_returns_nothing():
    """A novel's first chapter has no aliases yet. That must be an empty result, not a
    crash — the automaton refuses to build with no words."""
    assert _scan("Anything at all.", {}) == []


def test_empty_text_returns_nothing():
    assert _scan("", {"e1": "Chen"}) == []


def test_absent_surface_is_not_reported():
    assert _scan("Nobody here.", {"e1": "Chen"}, lang="en") == []
