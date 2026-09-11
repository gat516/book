"""Resumable records backfill for chapters with saved prose.

This command only enqueues enrichment pointers.  It never calls TRANSLATE and therefore
cannot alter a saved translation.  A JSONL manifest records object hashes before and
after each pass so an operator can verify that source and display objects were preserved.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import psycopg
import redis.asyncio as redis
from minio import Minio

from pipeline.config import Config
from pipeline.envelope import QueueMessage
from pipeline import queue


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def object_hash(store: Minio, bucket: str, uri: str | None) -> tuple[str | None, int | None]:
    if not uri:
        return None, None
    response = store.get_object(bucket, uri)
    try:
        h = hashlib.sha256(); size = 0
        for chunk in response.stream(1024 * 1024):
            h.update(chunk); size += len(chunk)
        return h.hexdigest(), size
    finally:
        response.close(); response.release_conn()


async def backfill(novel_id: str, manifest: Path | None = None) -> int:
    cfg = Config.load()
    db = await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True)
    r = redis.from_url(cfg.redis_url, decode_responses=True)
    store = Minio(cfg.object_endpoint, access_key=cfg.object_access_key,
                  secret_key=cfg.object_secret_key, secure=cfg.object_secure)
    count = 0
    rows = await (await db.execute(
        "SELECT chapter_index, raw_hash, raw_uri, translated_uri FROM chapter "
        "WHERE novel_id=%s AND translation_ready=true ORDER BY chapter_index", (novel_id,)
    )).fetchall()
    out = manifest.open("a", encoding="utf-8") if manifest else None
    try:
        for chapter, raw_hash, raw_uri, translated_uri in rows:
            msg = QueueMessage(novel_id=novel_id, chapter_index=chapter, enrichment=True)
            inserted = await r.eval(queue.ENQUEUE_ENRICHMENT, len(queue.KEYS), *queue.KEYS,
                                    novel_id, chapter, msg.model_dump_json())
            if inserted:
                count += 1
            if out:
                raw_sha, raw_bytes = object_hash(store, cfg.object_bucket, raw_uri)
                translation_sha, translation_bytes = object_hash(store, cfg.object_bucket, translated_uri)
                # Read the objects on both sides of enqueue. No object is downloaded for
                # processing and neither object is ever rewritten by this command.
                raw_sha_after, raw_bytes_after = object_hash(store, cfg.object_bucket, raw_uri)
                translation_sha_after, translation_bytes_after = object_hash(store, cfg.object_bucket, translated_uri)
                out.write(json.dumps({"novel_id": novel_id, "chapter_index": chapter,
                                      "raw_hash": raw_hash, "raw_uri": raw_uri,
                                      "translated_uri": translated_uri,
                                      "raw_sha256_before": raw_sha, "raw_sha256_after": raw_sha_after,
                                      "raw_bytes_before": raw_bytes, "raw_bytes_after": raw_bytes_after,
                                      "translation_sha256_before": translation_sha,
                                      "translation_sha256_after": translation_sha_after,
                                      "translation_bytes_before": translation_bytes,
                                      "translation_bytes_after": translation_bytes_after,
                                      "objects_unchanged": raw_sha == raw_sha_after and translation_sha == translation_sha_after}, ensure_ascii=False) + "\n")
    finally:
        if out: out.close()
        await r.aclose()
        await db.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("novel_id")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    print(f"enqueued {asyncio.run(backfill(args.novel_id, args.manifest))} saved chapters")


if __name__ == "__main__":
    main()
