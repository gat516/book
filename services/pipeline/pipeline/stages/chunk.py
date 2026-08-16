"""Stage 1: CHUNK (instructions.md §5 step 1).

Splits ``raw_text`` into token-budgeted chunks for RAG retrieval (§4's ``chunk`` table).
Pure CPU, no I/O, no LLM: paragraph boundaries first (double newline), then sentence
boundaries within an over-budget paragraph, packing sentences into a token budget and
never splitting mid-sentence. Token count is estimated as
``len(text) / language_profile.chars_per_token`` — good enough for a packing budget,
not meant to match a real tokenizer.

Offsets (``char_start``/``char_end``) are against the SOURCE text, per §5: the display
spans used for the reader UI come from a second pass over the *translated* text in step
6 (1.7), which is deliberately a separate concern from where a chunk sits in the source.
"""

from __future__ import annotations

import logging

from pipeline.context import Chunk, LanguageProfile, PipelineState, StageContext

log = logging.getLogger(__name__)

TOKEN_BUDGET = 400  # chunk size target, in estimated tokens


def _estimate_tokens(text: str, profile: LanguageProfile) -> float:
    return len(text) / profile.chars_per_token


def _split_sentences(paragraph: str, profile: LanguageProfile) -> list[str]:
    """Split a paragraph into sentences, keeping the terminator attached to each one."""
    sentences: list[str] = []
    start = 0
    for i, ch in enumerate(paragraph):
        if ch in profile.sentence_terminators:
            sentences.append(paragraph[start : i + 1])
            start = i + 1
    if start < len(paragraph):
        sentences.append(paragraph[start:])
    return [s for s in sentences if s.strip()]


def chunk_text(raw_text: str, profile: LanguageProfile, *, budget: int = TOKEN_BUDGET) -> list[Chunk]:
    """Pure function: source text -> ordered, offset-tagged chunks.

    Packs paragraphs/sentences into ``budget`` estimated tokens per chunk. A single
    sentence larger than the budget still becomes its own chunk whole — the budget is
    a packing target, not a hard cap that would force splitting mid-sentence.
    """
    chunks: list[Chunk] = []
    cur_start: int | None = None
    cur_end = 0
    cur_tokens = 0.0

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_tokens
        if cur_start is not None:
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=raw_text[cur_start:cur_end],
                    char_start=cur_start,
                    char_end=cur_end,
                )
            )
        cur_start = None
        cur_end = 0
        cur_tokens = 0.0

    pos = 0
    for paragraph in raw_text.split("\n\n"):
        para_start = raw_text.index(paragraph, pos) if paragraph else pos
        pos = para_start + len(paragraph)
        if not paragraph.strip():
            continue

        for sentence in _split_sentences(paragraph, profile):
            sent_start = raw_text.index(sentence, para_start)
            sent_end = sent_start + len(sentence)
            sent_tokens = _estimate_tokens(sentence, profile)

            if cur_start is not None and cur_tokens + sent_tokens > budget:
                flush()
            if cur_start is None:
                cur_start = sent_start
            cur_end = sent_end
            cur_tokens += sent_tokens

    flush()
    return chunks


class ChunkStage:
    name = "chunk"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        state.chunks = chunk_text(state.envelope.raw_text, ctx.language_profile)
        log.debug(
            "stage %s chapter=%d chunks=%d",
            self.name,
            state.envelope.chapter_index,
            len(state.chunks),
        )
