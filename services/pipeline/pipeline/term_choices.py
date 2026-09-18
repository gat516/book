"""One provisional spelling per source term, reusing existing display alignment (§0).

This is terminology, never identity. No model calls, glossary locks, or automatic
approvals occur here. The first choice survives later mentions until the reader
approves or corrects it through the hovercard.
"""
from dataclasses import replace
import logging
import re

from psycopg.types.json import Jsonb
from pipeline.context import PipelineState, StageContext
from pipeline.display_names import TermRenderingOccurrence
from pipeline.evidence import digest, stable_id
from pipeline.glossary_locks import _lock_glossary
from pipeline.name_renderings import conventional_english_names
from pipeline.pinyin_names import GENERIC_TITLES, NameCandidate, NamePlan, plan_character_name

log = logging.getLogger(__name__)


def provisional_plan(surface: str, display: str, role: str, target_lang: str):
    rendering = {
        "chinese_person": "chinese_personal", "foreign_person": "foreign_personal",
        "personal_title": "titled_person", "semantic_term": "semantic_term",
    }.get(role)
    if rendering is None:
        return None
    # Preserve Pinyin surname/given-name formatting and conventional foreign-name
    # restoration. Titles and semantic terms keep their existing translated wording.
    plan = _rendering_plan(surface, rendering, [display], target_lang)
    if not plan.candidates:
        return None
    return replace(plan, candidates=plan.candidates[:1], auto_target=None)


async def record_term_choices(ctx, state, occurrences: list[TermRenderingOccurrence]) -> None:
    if ctx.novel.source_lang.split("-")[0] != "zh" or ctx.novel.source_lang == ctx.novel.target_lang:
        return
    seen = set()
    for occurrence in occurrences:
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
        return NamePlan(candidates, None, "restored_name", "foreign_person", "restored_name")
    if rendering == "chinese_personal" or surface in GENERIC_TITLES:
        # A Chinese personal name's literal meaning is NOT its display spelling.
        return plan_character_name(surface)
    if rendering == "semantic_term":
        candidates = tuple(NameCandidate(target, (), "", "semantic_translation")
                           for target in dict.fromkeys(targets))
        return NamePlan(candidates, None, "semantic_translation", "semantic_term", "semantic_translation")
    method = {"foreign_personal": "restored_name", "titled_person": "translated_title"}[rendering]
    role = {"foreign_personal": "foreign_person", "titled_person": "personal_title"}[rendering]
    candidates = tuple(NameCandidate(target, (), "", method) for target in dict.fromkeys(targets))
    # Never auto-approve a model's restoration, even when it offers only one spelling.
    return NamePlan(candidates, None, method, role, method)


def _evidence_for(source: str, start: int, end: int) -> str:
    left = max(source.rfind("。", 0, start), source.rfind("\n", 0, start)) + 1
    stop = source.find("。", end)
    right = min(len(source), stop + 1 if stop >= 0 else end + 180)
    return source[left:right]


async def _record_surface(ctx: StageContext, state: PipelineState, surface: str, plan: NamePlan | None = None, *, preserve_existing: bool = False) -> bool:
    source = state.envelope.raw_text
    chapter = state.envelope.chapter_index
    source_hash = digest(source)
    matches = list(re.finditer(re.escape(surface), source))
    if not matches:
        return False
    first = matches[0]
    quote = _evidence_for(source, first.start(), first.end())
    plan = plan or plan_character_name(surface)
    if plan.reason in {"generic_title", "not_simple_hanzi_name", "missing_pinyin"}:
        log.warning("character_names: rejected invalid character surface %r (%s)", surface, plan.reason)
        return False

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
    if plan.auto_target:
        version = await _lock_glossary(
            ctx.db, novel_id=ctx.novel.id, source_term=surface,
            target_term=plan.auto_target, entity_id=None, chapter=chapter,
            target_lang=ctx.novel.target_lang, require_corroboration=False,
            constraint_class="character_name",
        )
        if version is not None:
            await ctx.db.execute("""UPDATE character_name_review SET status='approved',
                selected_target=%s,selection_source='deterministic',reviewed_by='deterministic',reviewed_at=now(),updated_at=now()
                WHERE novel_id=%s AND source_term=%s""",
                (plan.auto_target, ctx.novel.id, surface))
            return False
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
