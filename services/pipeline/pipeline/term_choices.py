"""One provisional spelling per source term, reusing existing display alignment (§0).

This is terminology, never identity. No model calls, glossary locks, or automatic
approvals occur here. The first choice survives later mentions until the reader
approves or corrects it through the hovercard.
"""
from dataclasses import dataclass, replace
from itertools import islice, product
import logging
import re

from pypinyin import Style, pinyin

from psycopg.types.json import Jsonb
from pipeline.context import PipelineState, StageContext
from pipeline.display_names import TermRenderingOccurrence
from pipeline.evidence import digest, stable_id
from pipeline.name_renderings import conventional_english_names

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NameCandidate:
    target_term: str
    pronunciation: tuple[str, ...]
    segmentation: str
    method: str

    def as_dict(self) -> dict:
        return {"target_term": self.target_term, "pronunciation": list(self.pronunciation),
                "segmentation": self.segmentation, "method": self.method}


@dataclass(frozen=True)
class NamePlan:
    candidates: tuple[NameCandidate, ...]
    reason: str
    term_role: str
    rendering_method: str


_HANZI = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{1,6}$")


def _letters(word: str) -> str:
    # Tone-free comparison: pypinyin writes ü as v, a model may write ü, v or u.
    return word.lower().replace("ü", "u").replace("v", "u")


def _align(syllables: tuple[str, ...], words: list[str]) -> tuple[int, list[tuple[str, ...]]]:
    """The longest tail of ``words`` spelled by a tail of ``syllables``.

    Returns how many leading syllables are left unmatched, and the matched syllable
    groups, one per word. "An Ruosu" over an·ruo·su matches everything; "Dragon Fei"
    over long·fei matches only "Fei", leaving the surname unmatched.
    """
    best: tuple[int, list[tuple[str, ...]]] = (len(syllables), [])

    def walk(end: int, word: int, groups: list[tuple[str, ...]]) -> None:
        nonlocal best
        if end < best[0]:
            best = (end, groups)
        if word == 0 or end == 0:
            return
        for start in range(end - 1, -1, -1):
            if _letters("".join(syllables[start:end])) == _letters(words[word - 1]):
                walk(start, word - 1, [syllables[start:end], *groups])

    walk(len(syllables), len(words), [])
    return best


def _pinyin_name(surface: str, display: str) -> tuple[NameCandidate, int, int] | None:
    """A Chinese name's spelling: letters from the characters, spacing from the model.

    The letters come from pypinyin, so a name can never come out translated by
    meaning. Only where the spaces go comes from the model, which knows where a surname
    ends ("An Ruosu", two-character "Longze Liyue") far better than a surname list.
    Leading syllables the model did not spell in Pinyin become one word: that is the
    surname in "Dragon Fei". Also returns how many of the model's words matched and how
    many leading syllables did not.
    """
    if not _HANZI.fullmatch(surface):
        return None
    readings = pinyin(surface, style=Style.NORMAL, heteronym=True, errors="ignore", strict=True,
                      v_to_u=True)
    if len(readings) != len(surface) or any(not row for row in readings):
        return None
    words = re.findall(r"[^\W\d_]+", display.replace("'", "").replace("\u2019", ""))
    best = None
    # Polyphonic characters: prefer the reading the model's spelling agrees with.
    for syllables in islice(product(*(tuple(dict.fromkeys(row)) for row in readings)), 64):
        unmatched, groups = _align(syllables, words)
        if best is None or unmatched < best[0][0]:
            best = ((unmatched, groups), syllables)
    (unmatched, groups), syllables = best
    parts = ([syllables[:unmatched]] if unmatched else []) + groups
    target = " ".join("".join(part).capitalize() for part in parts)
    segmentation = "mononym" if len(parts) == 1 else "surname+given" if len(parts) == 2 else "split"
    return NameCandidate(target, syllables, segmentation, "pinyin"), len(groups), unmatched


def provisional_plan(surface: str, display: str, role: str, target_lang: str):
    spelled = _pinyin_name(surface, display) if display.strip() else None
    if role in ("personal_title", "semantic_term") and spelled:
        _, matched, leading = spelled
        # "Dragon Fei" labelled a title: the model spelled the given name in Pinyin but
        # translated a one- or two-character surname. A real title keeps the whole name
        # in Pinyin (龍飛大人, "Lord Long Fei") and a term translated by meaning has no
        # Pinyin in it (龍王, "Dragon King"), so neither matches this.
        if matched and 1 <= leading <= 2 and len(re.findall(r"[^\W\d_]+", display)) > matched:
            role = "chinese_person"
    if role == "chinese_person":
        if spelled:
            return NamePlan((spelled[0],), "pinyin", "chinese_person", "pinyin")
        # Not plain hanzi (a mixed or transcribed form): the model's spelling as written.
        if not display.strip():
            return None
        return NamePlan((NameCandidate(display.strip(), (), "", "pinyin"),),
                        "model_spelling", "chinese_person", "pinyin")
    rendering = {
        "chinese_person": "chinese_personal", "foreign_person": "foreign_personal",
        "personal_title": "titled_person", "semantic_term": "semantic_term",
    }.get(role)
    if rendering is None:
        return None
    # Conventional foreign-name restoration; titles and semantic terms keep the model's
    # translated wording.
    plan = _rendering_plan(surface, rendering, [display], target_lang)
    if not plan.candidates:
        return None
    return replace(plan, candidates=plan.candidates[:1])


async def record_term_choices(ctx, state, occurrences: list[TermRenderingOccurrence]) -> None:
    if ctx.novel.source_lang.split("-")[0] != "zh" or ctx.novel.source_lang == ctx.novel.target_lang:
        return
    # A title built on a person's name ("Grandpa Long Fei", 龍飛爺爺) is not a term of its
    # own: only the name inside it is. Otherwise the title gets its own spelling, review
    # entry and highlight, and wins the leftmost-longest scan over the name it contains.
    people = {o.source_term for o in occurrences if o.term_role in ("chinese_person", "foreign_person")}
    rows = await (await ctx.db.execute(
        "SELECT source_term FROM character_name_review WHERE novel_id=%s "
        "AND term_role IN ('chinese_person','foreign_person')", (ctx.novel.id,))).fetchall()
    people.update(row[0] for row in rows)
    seen = set()
    for occurrence in occurrences:
        if occurrence.term_role == "personal_title" and any(
                name != occurrence.source_term and name in occurrence.source_term for name in people):
            continue
        # "aligned" comes from display alignment, "source_names" from the pre-translation
        # source pass; glossary-scan occurrences are already decided and never re-proposed.
        if occurrence.source_term in seen or occurrence.method not in ("aligned", "source_names"):
            continue
        seen.add(occurrence.source_term)
        plan = provisional_plan(occurrence.source_term, occurrence.display_term,
                                occurrence.term_role, ctx.novel.target_lang)
        if plan is not None:
            await _record_surface(ctx, state, occurrence.source_term, plan, preserve_existing=True)


def _rendering_plan(surface: str, rendering: str, targets: list[str], target_lang: str = "en") -> NamePlan:
    conventional = conventional_english_names(surface) if target_lang.split("-")[0] == "en" else ()
    if conventional:
        suggestions = tuple(targets) if rendering == "foreign_personal" else ()
        candidates = tuple(NameCandidate(target, (), "", "restored_name")
                           for target in dict.fromkeys(conventional + suggestions))[:8]
        return NamePlan(candidates, "restored_name", "foreign_person", "restored_name")
    if rendering == "semantic_term":
        candidates = tuple(NameCandidate(target, (), "", "semantic_translation")
                           for target in dict.fromkeys(targets))
        return NamePlan(candidates, "semantic_translation", "semantic_term", "semantic_translation")
    method = {"foreign_personal": "restored_name", "titled_person": "translated_title"}[rendering]
    role = {"foreign_personal": "foreign_person", "titled_person": "personal_title"}[rendering]
    candidates = tuple(NameCandidate(target, (), "", method) for target in dict.fromkeys(targets))
    return NamePlan(candidates, method, role, method)


def _evidence_for(source: str, start: int, end: int) -> str:
    left = max(source.rfind("。", 0, start), source.rfind("\n", 0, start)) + 1
    stop = source.find("。", end)
    right = min(len(source), stop + 1 if stop >= 0 else end + 180)
    return source[left:right]


async def _record_surface(ctx: StageContext, state: PipelineState, surface: str, plan: NamePlan, *, preserve_existing: bool = False) -> bool:
    source = state.envelope.raw_text
    chapter = state.envelope.chapter_index
    source_hash = digest(source)
    matches = list(re.finditer(re.escape(surface), source))
    if not matches:
        return False
    first = matches[0]
    quote = _evidence_for(source, first.start(), first.end())

    row = await (await ctx.db.execute(
        "SELECT status,selected_target,jsonb_array_length(candidates)>0 FROM character_name_review WHERE novel_id=%s AND source_term=%s",
        (ctx.novel.id, surface),
    )).fetchone()
    existed = row is not None
    if row is None:
        await ctx.db.execute("""INSERT INTO character_name_review
            (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (ctx.novel.id, surface, chapter, source_hash, first.start(), first.end(), quote,
             Jsonb([candidate.as_dict() for candidate in plan.candidates]), plan.reason))
        row = await (await ctx.db.execute(
            "SELECT status,selected_target,jsonb_array_length(candidates)>0 FROM character_name_review WHERE novel_id=%s AND source_term=%s",
            (ctx.novel.id, surface),
        )).fetchone()
        status, selected = row[:2]
    else:
        status, selected = row[:2]

    for match in matches:
        occurrence_quote = _evidence_for(source, match.start(), match.end())
        occurrence_id = stable_id(ctx.novel.id, chapter, source_hash, match.start(), match.end(), "character-name")
        await ctx.db.execute("""INSERT INTO character_name_occurrence
            (id,novel_id,chapter_index,source_hash,source_term,char_start,char_end,quote)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (occurrence_id, ctx.novel.id, chapter, source_hash, surface,
             match.start(), match.end(), occurrence_quote))

    if status == "approved":
        return False
    if preserve_existing and existed and row[2]:
        return True
    await _refresh_pending(ctx.db, ctx.novel.id, surface, plan, chapter)
    return True


async def _refresh_pending(db, novel_id: str, surface: str, plan: NamePlan, chapter: int) -> None:
    # Refresh stale pinyin-only choices without changing approved spellings or using
    # a later chapter to change the evidence shown at an earlier reader gate (§0).
    await db.execute("""UPDATE character_name_review SET candidates=%s,reason=%s,
        term_role=%s,rendering_method=%s,updated_at=now()
        WHERE novel_id=%s AND source_term=%s AND status='pending' AND first_seen_chapter=%s""",
        (Jsonb([candidate.as_dict() for candidate in plan.candidates]), plan.reason,
         plan.term_role, plan.rendering_method,
         novel_id, surface, chapter))
