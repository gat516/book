"""Mention scanning — the Python side of ``proto/textproc.proto`` (instructions.md §3.3,
§5 step 2; PLAN.md 1.6).

These models mirror the proto message-for-message and field-for-field, the same way
``envelope.py`` mirrors the Go ``ChapterEnvelope``. That is the whole point of writing the
proto before any Rust exists: in Phase 4 ``scan_mentions`` becomes a gRPC call and the
call site in ``stages/scan.py`` does not change.

**Both offset sets are emitted, and the two implementations arrive at them from opposite
directions.** The Rust ``aho-corasick`` crate matches bytes natively and maps to chars;
pyahocorasick matches chars natively and maps to bytes. The output is identical either
way, which is exactly why the contract pins both — the display layer indexes UTF-8 by
character, and on the CJK text this system exists to process a byte offset used as a
character offset points into the middle of a codepoint.

**Leftmost-longest matching**, so 王国 wins over 王 and "Li Xiaoyao" over "Li". The Rust
crate spells this ``MatchKind::LeftmostLongest``; pyahocorasick spells it
``Automaton.iter_long()``. Same semantics, different API — noted here because the names
share nothing and a Phase-4 reviewer will look for the constant.

**Segmentation is not on this path** (§3.3). Aho-Corasick matches exact substrings without
word boundaries, and suppressing the false positives that creates (the surname 王 matched
inside the unrelated word 王国) is a *post-match boundary filter* selected by ``lang``.
Wiring a segmenter into the matcher is the specific mistake the spec calls out; the filter
belongs after matching, and does not exist yet.
"""

from __future__ import annotations

import ahocorasick
from pydantic import BaseModel, Field


class Alias(BaseModel):
    """One searchable surface and the alias row it came from."""

    alias_id: str
    surface: str


class Span(BaseModel):
    """One mention. Offsets are half-open ``[start, end)`` in both units."""

    alias_id: str
    byte_start: int
    byte_end: int
    char_start: int
    char_end: int


class MentionScanRequest(BaseModel):
    text: str
    aliases: list[Alias] = Field(default_factory=list)
    lang: str = ""  # BCP-47; selects the post-match boundary filter (§5.3), unused today


class MentionScanResponse(BaseModel):
    spans: list[Span] = Field(default_factory=list)


def whole_name(text: str, span: Span, lang: str) -> bool:
    """Reject Latin substring hits; CJK exact spans require contextual resolution."""
    if lang.split("-")[0] in {"zh", "ja", "ko"}:
        return True
    start,end=span.char_start,span.char_end
    surface=text[start:end]
    word=lambda ch: ch.isalnum() or ch=="_"
    return not ((surface and word(surface[0]) and start>0 and word(text[start-1]))
                or (surface and word(surface[-1]) and end<len(text) and word(text[end])))


def _byte_offsets(text: str) -> list[int]:
    """Cumulative byte offset of each character index, plus a final total.

    Computed once per scan rather than re-encoding a prefix per match, which would make
    scanning quadratic in chapter length — invisible on a test fixture, unpleasant on a
    900-chapter backfill.
    """
    offsets = [0] * (len(text) + 1)
    total = 0
    for i, ch in enumerate(text):
        total += len(ch.encode("utf-8"))
        offsets[i + 1] = total
    return offsets


def scan_mentions(request: MentionScanRequest) -> MentionScanResponse:
    """Find every alias surface in ``text``, leftmost-longest.

    A surface shared by several aliases (two characters genuinely called "Chen") yields
    one span per alias_id at the same offsets. That is not a defect to be resolved here —
    it is precisely the ambiguity RESOLVE disambiguates, and collapsing it in the scanner
    would throw away the candidate set before anything got to weigh it.
    """
    if not request.aliases or not request.text:
        return MentionScanResponse()

    # Several aliases can share a surface, so the automaton stores every alias_id for it.
    by_surface: dict[str, list[str]] = {}
    for alias in request.aliases:
        if alias.surface:
            by_surface.setdefault(alias.surface, []).append(alias.alias_id)

    if not by_surface:
        return MentionScanResponse()

    automaton = ahocorasick.Automaton()
    for surface, alias_ids in by_surface.items():
        automaton.add_word(surface, (surface, alias_ids))
    automaton.make_automaton()

    byte_at = _byte_offsets(request.text)
    spans: list[Span] = []
    # iter_long() is pyahocorasick's leftmost-longest mode; iter() would also yield the
    # 王-inside-王国 match, which is the false positive the boundary filter exists for.
    for end_index, (surface, alias_ids) in automaton.iter_long(request.text):
        char_end = end_index + 1  # iter_long reports an INCLUSIVE end index
        char_start = char_end - len(surface)
        spans.extend(
            Span(
                alias_id=alias_id,
                byte_start=byte_at[char_start],
                byte_end=byte_at[char_end],
                char_start=char_start,
                char_end=char_end,
            )
            for alias_id in alias_ids
        )
    return MentionScanResponse(spans=[span for span in spans if whole_name(request.text,span,request.lang)])
