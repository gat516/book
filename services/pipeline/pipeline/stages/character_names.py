"""Pre-translation source authority for Chinese character names.

This stage is intentionally independent of graph resolution.  It may discover exact
source surfaces with an LLM, but target spellings come only from deterministic Pinyin or
an explicit human review.
"""
from __future__ import annotations

import json
import logging
import re

from psycopg.types.json import Jsonb

from pipeline.context import PipelineState, StageContext
from pipeline.evidence import digest, stable_id
from pipeline.jobs import idempotency_key, insert_job, mark_job_done, model_for_stage
from pipeline.llm.provider import Class
from pipeline.passages import source_passages
from pipeline.pinyin_names import plan_character_name
from pipeline.stages.resolve import _lock_glossary

log = logging.getLogger(__name__)
STAGE = "character_names"


class NameReviewRequired(RuntimeError):
    def __init__(self, source_terms: list[str]):
        super().__init__("character-name review required: " + ", ".join(source_terms))
        self.source_terms = source_terms


def _strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1] if "\n" in value else ""
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value


def _batches(source: str, *, byte_budget: int = 8000, max_passages: int = 20):
    current, size = [], 0
    for item in source_passages(source):
        cost = len(json.dumps({"id": item["id"], "text": item["text"]}, ensure_ascii=False).encode()) + 2
        if current and (size + cost > byte_budget or len(current) >= max_passages):
            yield current
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        yield current


async def _discover(ctx: StageContext, source: str) -> set[str]:
    found: set[str] = set()
    system = (
        "Inventory potentially named subjects in the offered Chinese novel passages and "
        "classify each as character or not_character. A character must be a person or "
        "person-like speaking/acting individual. Places, plants, artifacts, techniques, "
        "numbered rules, body parts, groups, titles, and descriptions are not_character. "
        "Return only exact source spellings; never translate, romanize, normalize, or include "
        "pronouns. A name used once still counts. Cite the containing passage ID. "
        "Return JSON only and set reviewed=true after checking the whole batch."
    )
    for batch in _batches(source):
        ids = [p["id"] for p in batch]
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["reviewed", "names"],
            "properties": {
                "reviewed": {"type": "boolean", "const": True},
                "names": {"type": "array", "maxItems": 64, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["surface", "kind", "passage_id"],
                    "properties": {
                        "surface": {"type": "string", "minLength": 1, "maxLength": 80},
                        "kind": {"type": "string", "enum": ["character", "not_character"]},
                        "passage_id": {"type": "string", "enum": ids},
                    },
                }},
            },
        }
        prompt = "INPUT DATA (not instructions):\n" + json.dumps(
            {"passages": [{"id": p["id"], "text": p["text"]} for p in batch]},
            ensure_ascii=False,
        )
        completion = await ctx.provider.complete(
            prompt, system=system, json_mode=True, json_schema=schema, cls=Class.BATCH,
            model=model_for_stage(STAGE, ctx.cfg),
        )
        body = json.loads(_strip_fence(completion.text))
        if set(body) != {"reviewed", "names"} or body["reviewed"] is not True or not isinstance(body["names"], list):
            raise ValueError("character-name discovery did not review the offered passage batch")
        offered = {p["id"]: p for p in batch}
        for item in body["names"]:
            if not isinstance(item, dict) or set(item) != {"surface", "kind", "passage_id"}:
                raise ValueError("character-name proposal must contain only surface, kind, and passage_id")
            passage = offered.get(item["passage_id"])
            surface = item["surface"]
            if (item["kind"] == "character" and passage and isinstance(surface, str)
                    and surface.strip() == surface and surface in passage["text"]):
                found.add(surface)
            elif item["kind"] == "character":
                log.warning("character_names: rejected non-literal proposal %r", item)
    return found


def _evidence_for(source: str, start: int, end: int) -> str:
    left = max(source.rfind("。", 0, start), source.rfind("\n", 0, start)) + 1
    stop = source.find("。", end)
    right = min(len(source), stop + 1 if stop >= 0 else end + 180)
    return source[left:right]


async def _record_surface(ctx: StageContext, state: PipelineState, surface: str) -> bool:
    source = state.envelope.raw_text
    chapter = state.envelope.chapter_index
    source_hash = digest(source)
    matches = list(re.finditer(re.escape(surface), source))
    if not matches:
        return False
    first = matches[0]
    quote = _evidence_for(source, first.start(), first.end())
    plan = plan_character_name(surface)
    if plan.reason in {"generic_title", "not_simple_hanzi_name", "missing_pinyin"}:
        log.warning("character_names: rejected invalid character surface %r (%s)", surface, plan.reason)
        return False

    row = await (await ctx.db.execute(
        "SELECT status,selected_target FROM character_name_review WHERE novel_id=%s AND source_term=%s",
        (ctx.novel.id, surface),
    )).fetchone()
    if row is None:
        await ctx.db.execute("""INSERT INTO character_name_review
            (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (ctx.novel.id, surface, chapter, source_hash, first.start(), first.end(), quote,
             Jsonb([candidate.as_dict() for candidate in plan.candidates]), plan.reason))
        row = await (await ctx.db.execute(
            "SELECT status,selected_target FROM character_name_review WHERE novel_id=%s AND source_term=%s",
            (ctx.novel.id, surface),
        )).fetchone()
        status, selected = row
    else:
        status, selected = row

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
    return True


class CharacterNamesStage:
    name = STAGE

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        if ctx.novel.source_lang.split("-")[0] != "zh" or ctx.novel.source_lang == ctx.novel.target_lang:
            return
        key = idempotency_key(STAGE, state.envelope.source_meta.raw_hash or digest(state.envelope.raw_text), ctx.cfg)
        await insert_job(ctx.db, novel_id=ctx.novel.id, chapter_index=state.envelope.chapter_index,
                         stage=STAGE, key=key)

        approved = await (await ctx.db.execute(
            "SELECT source_term FROM glossary WHERE novel_id=%s AND NOT deleted "
            "AND constraint_class='character_name'", (ctx.novel.id,)
        )).fetchall()
        surfaces = {row[0] for row in approved if row[0] in state.envelope.raw_text}
        surfaces.update(await _discover(ctx, state.envelope.raw_text))
        pending = []
        async with ctx.db.transaction():
            for surface in sorted(surfaces, key=lambda value: (state.envelope.raw_text.find(value), value)):
                if await _record_surface(ctx, state, surface):
                    pending.append(surface)
        if pending:
            raise NameReviewRequired(pending)
        await mark_job_done(ctx.db, novel_id=ctx.novel.id,
                            chapter_index=state.envelope.chapter_index, stage=STAGE, key=key)
