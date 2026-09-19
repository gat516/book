"""Tagged facts: parse the model's `category | fact` lines and tie names to characters.

The model tags each fact with a wiki category; code, not the model, decides who a fact is
about. A known spelling in a fact is replaced by a marker holding the character's ID, so
the stored fact names the character, not a spelling that a later correction would leave
behind. Readers see the name's current spelling where the marker was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = frozenset({"intro", "alias", "relationship", "ability", "item", "affiliation", "status",
                        "place", "event"})

_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*([a-z]+)(?:\s*:\s*([a-z][a-z -]*?))?\s*\|\s*(.+?)\s*$", re.I)


@dataclass(frozen=True)
class TaggedFact:
    category: str
    kind: str | None
    text: str


def parse_tagged(text: str) -> list[TaggedFact]:
    """Tagged facts after the last `## ` heading. A line with an unknown category, or a
    relationship without a kind, is dropped on its own; the rest of the answer stands."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
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
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    return body.strip().lower() == "none"


def marker(character_id: str) -> str:
    return f"⟦{character_id}⟧"


def mark_names(text: str, spellings: dict[str, str]) -> tuple[str, list[str]]:
    """Replace each known spelling in `text` with its key's marker, leftmost-longest and
    whole words only, so "Long Fei" inside a longer known name is not a mention of its
    own. `spellings` maps a key (a character ID) to its spelling. Returns the marked text
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
