"""FACTS: one call per chapter writes its important facts, each tagged for the wiki.

Replaces RECORDS (.claude/plans/facts-stage.md). Each fact carries a category (intro,
relationship, ability, ...) and the wiki subjects it names -- characters, organizations,
places and items (0112, 0115): code finds known spellings and stores a marker with the
subject's ID in their place, so a later spelling correction reaches every fact. Wiki
pages are assembled from these when read.
Because nothing here depends on another chapter's output, chapters run independently.

Append-only (§0): a prompt is versioned by its file name, and a chapter that already has
facts for this prompt and source hash is skipped. A new prompt adds a new set.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pipeline.context import PipelineState, StageContext
from pipeline.jobs import model_for_stage
from pipeline.llm.provider import Class
from pipeline.tagged_facts import mark_names, marker, parse_names, parse_tagged, says_none

log = logging.getLogger(__name__)

PROMPT_VERSION = "tagged-facts-v3.txt"
SYSTEM = (Path(__file__).resolve().parent.parent / "prompts" / PROMPT_VERSION).read_text()

# Groq's free tier caps Qwen3.8 at 1,000 output tokens a minute and refuses any request
# whose max_tokens could exceed it. A chapter's facts ran ~460-500 tokens in testing.
GROQ_MAX_OUTPUT_TOKENS = 950
# Elsewhere thinking tokens count against the budget too: DeepSeek at "low" wrote 2.6k-6.4k
# per chapter and one ran past 8k, so leave generous room.
MAX_OUTPUT_TOKENS = 16000


def _least_thinking(provider_id: str, model: str) -> str | None:
    """The thinking level for facts on each backend.

    DeepSeek thinks at "low": with thinking off it attached a title to the wrong person
    (a subordinate written as "the First Divine Throne"), and one wrong fact becomes a
    wrong alias on a wiki page. Elsewhere the lowest setting each backend has.
    """
    if provider_id == "deepseek":
        return "low"
    if provider_id == "groq":
        return "none" if "qwen" in model.lower() else "low"  # gpt-oss cannot switch off
    if provider_id == "openrouter":
        return "low"
    return None  # Ollama, Anthropic, custom: no such argument


PERSON_ROLES = ("chinese_person", "foreign_person")


async def _mark_subjects(ctx: StageContext, chapter: int, facts,
                         classified: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Each fact's text with known subjects replaced by markers, and the IDs of the
    subjects it names in order. A subject is created for a source term the first time a
    fact names it.

    A known term is marked when its kind is known: a person from the names pass, a term
    that already has a subject, or a term whose spelling the model listed under Names as
    an organization, place or item (`classified`). Anything else -- a technique, a title,
    an unclassified term -- stays plain text.
    """
    rows = await (await ctx.db.execute(
        """SELECT r.source_term, r.first_seen_chapter, r.term_role,
                  COALESCE(g.target_term, r.selected_target, r.candidates->0->>'target_term')
             FROM character_name_review r
             LEFT JOIN glossary g ON g.novel_id=r.novel_id AND g.source_term=r.source_term AND NOT g.deleted
            WHERE r.novel_id=%s AND r.first_seen_chapter <= %s
           UNION ALL
           SELECT g.source_term, %s, NULL, g.target_term
             FROM glossary g
            WHERE g.novel_id=%s AND NOT g.deleted AND g.locked_at_chapter <= %s
              AND NOT EXISTS (SELECT 1 FROM character_name_review r
                               WHERE r.novel_id=g.novel_id AND r.source_term=g.source_term)""",
        (ctx.novel.id, chapter, chapter, ctx.novel.id, chapter))).fetchall()
    existing = dict(await (await ctx.db.execute(
        "SELECT source_term, kind FROM subject WHERE novel_id=%s", (ctx.novel.id,))).fetchall())
    listed = {name.casefold(): kind for kind, name in classified}
    first_seen, kinds, spellings = {}, {}, {}
    for term, seen, role, spelling in rows:
        if not spelling:
            continue
        kind = ("character" if role in PERSON_ROLES else existing.get(term)
                or listed.get(spelling.casefold()))
        if kind:
            first_seen[term], kinds[term], spellings[term] = seen, kind, spelling
    marked = [mark_names(fact.text, spellings) for fact in facts]
    named = sorted({term for _, terms in marked for term in terms})
    ids: dict[str, str] = {}
    if named:
        async with ctx.db.cursor() as cur:
            await cur.executemany(
                """INSERT INTO subject (novel_id, source_term, first_seen_chapter, kind) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (novel_id, source_term) DO NOTHING""",
                [(ctx.novel.id, term, first_seen[term], kinds[term]) for term in named])
        ids = {term: str(sid) for sid, term in await (await ctx.db.execute(
            "SELECT id, source_term FROM subject WHERE novel_id=%s AND source_term = ANY(%s)",
            (ctx.novel.id, named))).fetchall()}
    result = []
    for text, terms in marked:
        for term in terms:
            text = text.replace(marker(term), marker(ids[term]))
        result.append((text, [ids[term] for term in terms]))
    return result


class FactsStage:
    name = "facts"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        same_language = ctx.novel.source_lang == ctx.novel.target_lang
        text = state.translation if state.translation is not None else (
            state.envelope.raw_text if same_language else None)
        if not text or not text.strip():
            return  # nothing readable yet; the chapter is re-queued once it is translated
        chapter = state.envelope.chapter_index
        source_hash = state.envelope.source_meta.raw_hash
        done = await (await ctx.db.execute(
            "SELECT 1 FROM chapter_fact WHERE novel_id=%s AND chapter_index=%s "
            "AND prompt_version=%s AND source_hash=%s LIMIT 1",
            (ctx.novel.id, chapter, PROMPT_VERSION, source_hash))).fetchone()
        if done:
            return

        model = model_for_stage("facts", ctx.cfg, ctx.model_override)
        effort = _least_thinking(ctx.provider_id or ctx.cfg.llm_provider, model)
        completion = await ctx.provider.complete(
            text, system=SYSTEM, cls=Class.BATCH, model=model,
            max_output_tokens=GROQ_MAX_OUTPUT_TOKENS if (ctx.provider_id or ctx.cfg.llm_provider) == "groq"
            else MAX_OUTPUT_TOKENS, **({"reasoning_effort": effort} if effort else {}))
        facts = parse_tagged(completion.text)
        if not facts and says_none(completion.text):
            # An explicit "no important facts" is an answer: the chapter is done with 0.
            await ctx.db.execute(
                "UPDATE chapter SET facts_count=0 WHERE novel_id=%s AND chapter_index=%s",
                (ctx.novel.id, chapter))
            log.info("stage facts chapter=%s facts=0 (none important)", chapter)
            return
        if not facts:
            # Nothing is written, so the next pass over this chapter tries again. Only a
            # count is logged: model text never reaches a log or read path (§0).
            log.warning("facts: no usable facts chapter=%s output_tokens=%s", chapter,
                        completion.output_tokens)
            return
        async with ctx.db.transaction():
            marked = await _mark_subjects(ctx, chapter, facts, parse_names(completion.text))
            async with ctx.db.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO chapter_fact (novel_id, chapter_index, prompt_version, ordinal, text,
                                                 category, kind, subjects, source_hash, requested_model,
                                                 served_provider, served_model)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    [(ctx.novel.id, chapter, PROMPT_VERSION, i, text, fact.category, fact.kind, subjects,
                      source_hash, model, completion.served_provider, completion.served_model)
                     for i, (fact, (text, subjects)) in enumerate(zip(facts, marked))])
            # The reader-visible "enrichment done" marker (migration 0110): a count only,
            # never fact text.
            await ctx.db.execute(
                "UPDATE chapter SET facts_count=%s WHERE novel_id=%s AND chapter_index=%s",
                (len(facts), ctx.novel.id, chapter))
        log.info("stage facts chapter=%s facts=%s in=%s out=%s", chapter, len(facts),
                 completion.input_tokens, completion.output_tokens)
