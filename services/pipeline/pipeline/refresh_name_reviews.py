"""Offline repair of pending spelling suggestions; never approves or queues work.

Uses each review's own original quote, not later chapters (§0 spoiler boundaries).
Run with --apply to persist suggestions; approved names are never changed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import psycopg

from pipeline.config import Config
from pipeline.context import NovelMeta
from pipeline.llm import provider_from_env
from pipeline.provider_config import build_provider, load_provider_config
from pipeline.stages.character_names import _discover, _refresh_pending


async def refresh_reviews(ctx, *, source_term: str | None = None, apply: bool = False) -> list[dict]:
    rows = await (await ctx.db.execute("""SELECT source_term,first_seen_chapter,quote
        FROM character_name_review WHERE novel_id=%s AND status='pending'
        AND (%s::text IS NULL OR source_term=%s) ORDER BY first_seen_chapter,source_term""",
        (ctx.novel.id, source_term, source_term))).fetchall()
    results = []
    for surface, chapter, quote in rows:
        plans = await _discover(ctx, quote)
        plan = plans.get(surface)
        if plan is None or not plan.candidates:
            # Absence/reclassification isn't permission to delete an outstanding review.
            results.append({"source_term": surface, "updated": False, "reason": "no_character_suggestions"})
            continue
        if apply:
            await _refresh_pending(ctx.db, ctx.novel.id, surface, plan, chapter)
        results.append({"source_term": surface, "applied": apply,
                        "candidates": [candidate.as_dict() for candidate in plan.candidates]})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel-id", required=True, type=UUID)
    parser.add_argument("--source-term", help="Refresh only this exact pending source spelling")
    parser.add_argument("--apply", action="store_true", help="Save suggestions, without approving them")
    args = parser.parse_args()

    async def run():
        cfg = Config.load()
        novel_id = str(args.novel_id)
        async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
            row = await (await db.execute(
                "SELECT source_lang,target_lang,ontology FROM novel WHERE id=%s", (novel_id,)
            )).fetchone()
            if row is None:
                raise ValueError("novel not found")
            novel = NovelMeta(novel_id, *row)
            if novel.source_lang.split("-")[0] != "zh" or novel.source_lang == novel.target_lang:
                raise ValueError("name review refresh requires a translated Chinese-source novel")
            config = await load_provider_config(db, novel_id)
            if cfg.llm_provider == "gateway":
                provider = provider_from_env(cfg, tenant=novel_id)
            else:
                provider = build_provider(config, cfg) if config else provider_from_env(cfg)
            ctx = SimpleNamespace(db=db, novel=novel, cfg=cfg, provider=provider)
            results = await refresh_reviews(ctx, source_term=args.source_term, apply=args.apply)
            print(json.dumps(results, ensure_ascii=False, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    main()
