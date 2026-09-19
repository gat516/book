"""FACTS: one call per chapter writes its important facts, each tagged for the wiki.

Replaces RECORDS (.claude/plans/facts-stage.md). Each fact carries a category (intro,
relationship, ability, ...) and the characters it names: code finds known spellings and
stores a marker with the character's ID in their place (migration 0112), so a later
spelling correction reaches every fact. Wiki pages are assembled from these when read.
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
from pipeline.tagged_facts import mark_names, marker, parse_tagged, says_none

log = logging.getLogger(__name__)

PROMPT_VERSION = "tagged-facts-v2.txt"
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


async def _mark_characters(ctx: StageContext, chapter: int, facts) -> list[tuple[str, list[str]]]:
    """Each fact's text with known people replaced by character markers, and the IDs of
    the characters it names in order. A character is created for a person's source term
    the first time a fact names them."""
    rows = await (await ctx.db.execute(
        """SELECT r.source_term, r.first_seen_chapter,
                  COALESCE(g.target_term, r.selected_target, r.candidates->0->>'target_term')
             FROM character_name_review r
             LEFT JOIN glossary g ON g.novel_id=r.novel_id AND g.source_term=r.source_term AND NOT g.deleted
            WHERE r.novel_id=%s AND r.first_seen_chapter <= %s
              AND r.term_role IN ('chinese_person','foreign_person')""",
        (ctx.novel.id, chapter))).fetchall()
    first_seen = {term: seen for term, seen, _ in rows}
    spellings = {term: spelling for term, _, spelling in rows if spelling}
    marked = [mark_names(fact.text, spellings) for fact in facts]
    named = sorted({term for _, terms in marked for term in terms})
    ids: dict[str, str] = {}
    if named:
        async with ctx.db.cursor() as cur:
            await cur.executemany(
                """INSERT INTO character (novel_id, source_term, first_seen_chapter) VALUES (%s,%s,%s)
                   ON CONFLICT (novel_id, source_term) DO NOTHING""",
                [(ctx.novel.id, term, first_seen[term]) for term in named])
        ids = {term: str(cid) for cid, term in await (await ctx.db.execute(
            "SELECT id, source_term FROM character WHERE novel_id=%s AND source_term = ANY(%s)",
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
            marked = await _mark_characters(ctx, chapter, facts)
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
