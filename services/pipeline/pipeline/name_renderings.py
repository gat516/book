"""Conventional English spelling *candidates* for common Chinese transcriptions.

Language data, not a novel ontology or an identity dictionary. These ambiguous surface
forms always require review, including when a model mislabels them Chinese personal
names. Unlisted names still use contextual model suggestions; never guess an identity
or translate a Chinese personal name's literal meaning from this table.
"""
from itertools import product
import re


ENGLISH_TRANSCRIPTIONS: dict[str, tuple[str, ...]] = {
    "劳伦斯": ("Lawrence", "Laurence"),
    "芙蕾雅": ("Freya", "Freyja"),
    "爱丽丝": ("Alice",),
    "威廉": ("William",),
    "爱德华": ("Edward",),
    "约翰": ("John",),
    "玛丽": ("Mary", "Marie"),
    "安娜": ("Anna", "Ana"),
    "亚历山大": ("Alexander",),
    "查尔斯": ("Charles",),
    "詹姆斯": ("James",),
    "伊丽莎白": ("Elizabeth",),
    "罗伯特": ("Robert",),
    "托马斯": ("Thomas",),
    "诺顿": ("Norton",),
    "威尔森": ("Wilson",),
    "马克": ("Mark", "Marc"),
    "艾伦": ("Alan", "Allen", "Ellen"),
}


def conventional_english_names(surface: str) -> tuple[str, ...]:
    if surface in ENGLISH_TRANSCRIPTIONS:
        return ENGLISH_TRANSCRIPTIONS[surface]
    # Middle dots mark name components. Restore only when EVERY component is known;
    # otherwise leave the whole spelling to context and human review.
    parts = re.split(r"[·•・]", surface)
    if 1 < len(parts) <= 4 and all(part in ENGLISH_TRANSCRIPTIONS for part in parts):
        return tuple(" ".join(parts) for parts in product(*(ENGLISH_TRANSCRIPTIONS[p] for p in parts)))[:8]
    return ()
