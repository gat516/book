"""CHUNK INDEX: write the chapter's target-language chunks and embeddings for askai.

The chapter's chunk rows are replaced as a derived index, and embeddings are
best-effort -- an unavailable or mismatched embedder stores the chunk with a NULL vector
rather than failing the chapter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from pgvector.psycopg import register_vector_async

from pipeline.context import PipelineState, StageContext

log = logging.getLogger(__name__)


async def _embedding_rows(ctx: StageContext, chunks: Sequence[Any]) -> list[list[float] | None]:
    """Best-effort chunk embeddings; never make a chapter depend on retrieval.

    Chunk vectors are only a derived semantic-retrieval index, so an unavailable provider
    or a model width mismatch leaves the chunk row present with ``embedding IS NULL``.
    This also avoids poisoning a pgvector column with a vector from a changed model (§0
    append-only).
    """
    if not chunks:
        return []
    try:
        vectors = await ctx.embed_provider.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks")
        expected = ctx.cfg.embed_dim
        if any(not isinstance(vector, (list, tuple)) or len(vector) != expected for vector in vectors):
            raise ValueError(f"embedding provider returned a vector with unexpected width (expected {expected})")
        return [list(vector) for vector in vectors]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — retrieval-only degradation
        log.warning("chunk embeddings unavailable; indexing chunks without vectors: %s", type(exc).__name__)
        return [None] * len(chunks)


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
