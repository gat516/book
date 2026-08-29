"""Queue saved chapters for source-name discovery and selective replacement.

Usage: ``python -m pipeline.name_backfill NOVEL_ID``. Existing translations remain
readable. The worker's translation fingerprint makes chapters with no newly approved
name constraints cheap cache hits; ambiguous names stop at review before any rewrite.
"""
from __future__ import annotations

import argparse
import asyncio

import psycopg
import redis.asyncio as aredis

from pipeline import queue
from pipeline.config import Config
from pipeline.envelope import QueueMessage


async def enqueue(novel_id: str) -> list[int]:
    cfg = Config.load()
    client = aredis.from_url(cfg.redis_url, decode_responses=True)
    db = await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True)
    try:
        rows = await (await db.execute(
            "SELECT chapter_index FROM chapter WHERE novel_id=%s AND translation_ready "
            "ORDER BY chapter_index", (novel_id,)
        )).fetchall()
        queued = []
        for (chapter,) in rows:
            msg = QueueMessage(novel_id=novel_id, chapter_index=chapter, retranslate=True)
            added = await client.eval(
                queue.ENQUEUE_ENRICHMENT, len(queue.KEYS), *queue.KEYS,
                novel_id, chapter, msg.model_dump_json(),
            )
            if added:
                queued.append(chapter)
        return queued
    finally:
        await db.close()
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="queue source-name repair for saved translations")
    parser.add_argument("novel_id")
    args = parser.parse_args()
    chapters = asyncio.run(enqueue(args.novel_id))
    print(f"queued {len(chapters)} chapters: {chapters}")


if __name__ == "__main__":
    main()
