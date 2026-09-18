"""CHUNK INDEX: write the chapter's target-language chunks and embeddings for askai.

These rows used to be written only inside RECORDS' publication transaction
(records_publish.py). With RECORDS out of the runtime (.claude/plans/facts-stage.md) this
stage keeps askai's retrieval index filled. Same semantics as before: the chapter's
chunk rows are replaced as a derived index, and embeddings are best-effort -- an
unavailable or mismatched embedder stores the chunk with a NULL vector rather than
failing the chapter.
"""

from __future__ import annotations

import logging

from pgvector.psycopg import register_vector_async

from pipeline.context import PipelineState, StageContext
from pipeline.records_publish import _embedding_rows

log = logging.getLogger(__name__)


class ChunkIndexStage:
    name = "chunk_index"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        if ctx.novel.source_lang != ctx.novel.target_lang and state.translation is None:
            return  # untranslated: its chunks are source text, which askai must not index
        chunks = state.chunks
        embeddings = await _embedding_rows(ctx, chunks)
        await register_vector_async(ctx.db)
        chapter = state.envelope.chapter_index
        async with ctx.db.transaction():
            await ctx.db.execute("DELETE FROM chunk WHERE novel_id=%s AND chapter_index=%s",
                                 (ctx.novel.id, chapter))
            if chunks:
                async with ctx.db.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO chunk (novel_id,chapter_index,text,embedding,embedding_space) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        [(ctx.novel.id, chapter, c.text, e, ctx.embedding_space if e is not None else None)
                         for c, e in zip(chunks, embeddings)])
        log.info("stage chunk_index chapter=%s chunks=%s embedded=%s", chapter, len(chunks),
                 sum(e is not None for e in embeddings))
