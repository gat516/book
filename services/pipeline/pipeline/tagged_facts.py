"""Tagged facts: parse the model's `category | fact` lines and tie names to wiki subjects.

The model tags each fact with a wiki category, and lists the kind (organization, place,
item) of each named thing its facts mention; code, not the model, decides who and what a
fact is about. A known spelling in a fact is replaced by a marker holding the subject's
ID, so the stored fact names the subject, not a spelling that a later correction would
leave behind. Readers see the name's current spelling where the marker was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = frozenset({"intro", "alias", "relationship", "ability", "item", "affiliation", "status",
                        "place", "event"})

# The kinds a named non-person can have; people come from the names pass (0115).
NAME_KINDS = frozenset({"organization", "place", "item"})

_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*([a-z]+)(?:\s*:\s*([a-z][a-z -]*?))?\s*\|\s*(.+?)\s*$", re.I)


@dataclass(frozen=True)
class TaggedFact:
    category: str
    kind: str | None
    text: str


def _section(text: str, name: str) -> str | None:
    """The lines under `## <name>`, up to the next `## ` heading; None if it's absent."""
    heading = None
    for heading in re.finditer(rf"^##\s*{name}\s*$", text, re.M | re.I):
        pass  # the last one: a model may think aloud with an earlier draft
    if heading is None:
        return None
    rest = text[heading.end():]
    following = re.search(r"^## ", rest, re.M)
    return rest[:following.start()] if following else rest


def _facts_body(text: str) -> str:
    body = _section(text, "Facts")
    if body is not None:
        return body
    headings = list(re.finditer(r"^## .*$", text, re.M))  # tagged-facts-v2: one section
    return text[headings[-1].end():] if headings else text


def parse_tagged(text: str) -> list[TaggedFact]:
    """Tagged facts from the `## Facts` section. A line with an unknown category, or a
    relationship without a kind, is dropped on its own; the rest of the answer stands."""
    body = _facts_body(text)
    facts = []
    for raw in body.splitlines():
        match = _LINE.match(raw)
        if not match:
            continue
        category, kind = match.group(1).lower(), (match.group(2) or "").strip().lower() or None
        if category not in CATEGORIES or (category == "relationship") != (kind is not None):
            continue
        facts.append(TaggedFact(category, kind, match.group(3)))
    return facts


def says_none(text: str) -> bool:
    """The model's explicit "no important facts" answer: `## Facts` then `None`."""
    return _facts_body(text).strip().lower() == "none"


_NAME = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*([a-z]+)\s*\|\s*(.+?)\s*$", re.I)


def parse_names(text: str) -> list[tuple[str, str]]:
    """(kind, name) pairs from the `## Names` section. A line with an unknown kind is
    dropped on its own."""
    names = []
    for raw in (_section(text, "Names") or "").splitlines():
        match = _NAME.match(raw)
        if match and match.group(1).lower() in NAME_KINDS:
            names.append((match.group(1).lower(), match.group(2)))
    return names


def marker(character_id: str) -> str:
    return f"⟦{character_id}⟧"


def mark_names(text: str, spellings: dict[str, str]) -> tuple[str, list[str]]:
    """Replace each known spelling in `text` with its key's marker, leftmost-longest and
    whole words only, so "Long Fei" inside a longer known name is not a mention of its
    own. `spellings` maps a key (a subject ID) to its spelling. Returns the marked text
    and the keys in the order they first appear: for "<X> is <Y>'s <kind>", X then Y."""
    spans: list[tuple[int, int, str]] = []
    taken = [False] * len(text)
    for key, spelling in sorted(spellings.items(), key=lambda item: -len(item[1])):
        if not spelling:
            continue
        for found in re.finditer(rf"(?<![\w-]){re.escape(spelling)}(?![\w-])", text):
            if not any(taken[found.start():found.end()]):
                spans.append((found.start(), found.end(), key))
                taken[found.start():found.end()] = [True] * (found.end() - found.start())
    spans.sort()
    pieces, order, at = [], [], 0
    for start, end, key in spans:
        pieces += [text[at:start], marker(key)]
        at = end
        if key not in order:
            order.append(key)
    return "".join(pieces) + text[at:], order
