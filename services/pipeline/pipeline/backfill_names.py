"""Index names in saved, completed chapters without retranslating or changing facts.

Usage: python -m pipeline.backfill_names --novel UUID --start 1 --end 18 [--apply]
Without --apply, discovery runs but no chapter data is changed. Model results are cached.
Existing links are preserved, never inferred from spelling or a future glossary (§0.3).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import replace

import psycopg

from pipeline.config import Config
from pipeline.context import NovelMeta, StageContext, language_profile_for
from pipeline.display_names import align_names, discover_names, merge_names
from pipeline.graph import GraphWriter
from pipeline.mentions import Span
from pipeline.worker import Worker


async def backfill(novel_id: str, start: int, end: int, apply: bool) -> None:
    worker = Worker(Config.load())
    worker.db = await psycopg.AsyncConnection.connect(worker.cfg.database_url, autocommit=True)
    try:
        row = await worker._fetch_one(
            "SELECT source_lang, target_lang, ontology FROM novel WHERE id=%s", (novel_id,))
        if row is None:
            raise ValueError("novel not found")
        source, target, ontology = row
        (provider, batches, provider_id, names_provider, _resolve_provider,
         model_override) = await worker._provider_for_novel(novel_id)
        ctx = StageContext(
            novel=NovelMeta(novel_id, source, target, ontology),
            language_profile=language_profile_for(source), provider=provider,
            batch_manager=batches, embed_provider=worker.embed_provider, db=worker.db,
            objects=worker.minio, cfg=worker.cfg, cache=worker.cache,
            textproc=worker.textproc, provider_id=provider_id, names_provider=names_provider,
            model_override=model_override,
        )
        chapters = await (await worker.db.execute(
            "SELECT chapter_index, raw_uri, COALESCE(translated_uri, raw_uri), translated_uri IS NOT NULL FROM chapter "
            "WHERE novel_id=%s AND status='done' AND chapter_index BETWEEN %s AND %s ORDER BY chapter_index",
            (novel_id, start, end),
        )).fetchall()
        for index, raw_uri, uri, translated in chapters:
            source_text = await asyncio.to_thread(worker._get_object, raw_uri)
            text = await asyncio.to_thread(worker._get_object, uri)
            # Legacy completed chapters can still display raw text. Use that language
            # for word boundaries, just as the reader uses raw_uri as its fallback.
            display_ctx = ctx if translated else replace(ctx, novel=replace(ctx.novel, target_lang=source))
            names = await discover_names(display_ctx, text)
            rows = await (await worker.db.execute(
                "SELECT entity_id, char_start, char_end FROM mention_span WHERE novel_id=%s AND chapter_index=%s",
                (novel_id, index),
            )).fetchall()
            existing = [Span(alias_id=str(entity) if entity else "", char_start=a, char_end=b,
                             byte_start=0, byte_end=0) for entity, a, b in rows]
            merged = merge_names(existing, names)
            renderings = await align_names(display_ctx, source_text, text, merged)
            async with worker.db.transaction():
                # Don't publish offsets if another process replaced this text while the
                # model worked. Lock only during the short persistence transaction.
                current = await worker._fetch_one(
                    "SELECT status, COALESCE(translated_uri, raw_uri) FROM chapter "
                    "WHERE novel_id=%s AND chapter_index=%s FOR UPDATE", (novel_id, index))
                if current != ("done", uri):
                    raise RuntimeError(f"chapter {index} changed during indexing; retry")
                if apply:
                    await GraphWriter(worker.db).replace_mention_spans(
                        novel_id, index, merged, renderings)
            print(json.dumps({"chapter": index, "added": len(merged)-len(existing),
                              "total": len(merged), "aligned": len(renderings),
                              "applied": apply}), flush=True)
    finally:
        await worker.db.close()
        await worker.redis.aclose()
        await worker.textproc.aclose()
        providers = [worker._default_provider, worker.embed_provider]
        providers.extend(p for item in worker._provider_cache.values() for p in (item[0], item[3]) if p)
        for provider in {id(p): p for p in providers}.values():
            if hasattr(provider, "aclose"):
                await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", type=uuid.UUID, required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.start < 1 or args.end < args.start:
        parser.error("require 1 <= start <= end")
    asyncio.run(backfill(str(args.novel), args.start, args.end, args.apply))
