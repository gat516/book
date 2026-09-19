"""FACTS: one call per chapter writes its important facts as short story summaries.

Replaces RECORDS (.claude/plans/facts-stage.md). Facts are untagged plain sentences,
stored per chapter in ``chapter_fact`` and hidden from readers; wiki pages are written
from them later, and that later step decides identities. Because nothing here depends on
another chapter's output, chapters run independently: no ordering wait, no generation.

Append-only (§0): a prompt is versioned by its file name, and a chapter that already has
facts for this prompt and source hash is skipped. A new prompt adds a new set.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pipeline.context import PipelineState, StageContext
from pipeline.jobs import model_for_stage
from pipeline.llm.provider import Class

log = logging.getLogger(__name__)

PROMPT_VERSION = "story-facts-v2.txt"
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


def parse_facts(text: str) -> list[str]:
    """Lines after the last `## ` heading, with bullets or numbering stripped."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    lines = (re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip() for raw in body.splitlines())
    return [line for line in lines if line]


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
        facts = parse_facts(completion.text)
        if not facts:
            # Nothing is written, so the next pass over this chapter tries again. Only a
            # count is logged: model text never reaches a log or read path (§0).
            log.warning("facts: no usable facts chapter=%s output_tokens=%s", chapter,
                        completion.output_tokens)
            return
        async with ctx.db.transaction():
            async with ctx.db.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO chapter_fact (novel_id, chapter_index, prompt_version, ordinal, text,
                                                 source_hash, requested_model, served_provider, served_model)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    [(ctx.novel.id, chapter, PROMPT_VERSION, i, fact, source_hash, model,
                      completion.served_provider, completion.served_model)
                     for i, fact in enumerate(facts)])
            # The reader-visible "enrichment done" marker (migration 0110): a count only,
            # never fact text.
            await ctx.db.execute(
                "UPDATE chapter SET facts_count=%s WHERE novel_id=%s AND chapter_index=%s",
                (len(facts), ctx.novel.id, chapter))
        log.info("stage facts chapter=%s facts=%s in=%s out=%s", chapter, len(facts),
                 completion.input_tokens, completion.output_tokens)
