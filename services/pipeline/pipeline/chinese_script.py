"""Find a model-written Chinese term in the source, whichever script it was written in.

Models asked to copy a name from a Traditional Chinese chapter sometimes answer in
Simplified (星莲 for 星蓮, 裁决会 for 裁決會), and the reverse. That is the same word, not an
invention, so the exact-substring checks that stop invented names would wrongly drop it.
This converts the term to the other script and accepts it only if that form occurs
verbatim; the caller then stores the SOURCE's form, so glossary lookups and priming keep
matching the chapter exactly. A different character (燕 for 琰) is still rejected.
"""

from __future__ import annotations

from functools import lru_cache

from opencc import OpenCC


@lru_cache(maxsize=None)
def _converter(config: str) -> OpenCC:
    return OpenCC(config)


def source_form(term: str, source: str) -> str | None:
    """``term`` as it is written in ``source``, or None when neither script occurs there."""
    if term and term in source:
        return term
    for config in ("s2t", "t2s"):
        converted = _converter(config).convert(term)
        if converted and converted != term and converted in source:
            return converted
    return None
