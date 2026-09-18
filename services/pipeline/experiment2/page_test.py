"""Write one wiki page from story facts up to a chapter; prints the page. Writes nothing to Postgres.

Only facts from chapters <= --at are sent (§0: the page never sees later chapters). The
glossary spellings are passed as a hint; deciding which names are the same character is
left to the model.
"""
from __future__ import annotations

import argparse
import asyncio
import json

import psycopg

from facts import HERE, make_provider
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import resolve_provider_config


async def main(args):
    facts = json.loads((HERE / "results" / args.facts / "facts.json").read_text())
    lines = [f"[ch{ch}] {fact}" for ch, v in sorted(facts.items(), key=lambda kv: int(kv[0]))
             if int(ch) <= args.at for fact in v["facts"]]
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url) as db:
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        glossary = await (await db.execute(
            "SELECT source_term, target_term FROM glossary WHERE novel_id=%s AND NOT deleted "
            "AND locked_at_chapter <= %s ORDER BY source_term", (args.novel, args.at))).fetchall()
    prompt = (f"CHARACTER: {args.character}\n\n"
              + "KNOWN SPELLINGS:\n" + "\n".join(f"{s} = {t}" for s, t in glossary) + "\n\n"
              + f"FACTS (chapters 1-{args.at}):\n" + "\n".join(lines))
    provider = make_provider(args, row, cfg)
    try:
        c = await provider.complete(prompt, system=(HERE / "prompts" / args.prompt).read_text(),
                                    cls=Class.BATCH, model=args.model, max_output_tokens=args.max_output_tokens,
                                    reasoning_effort=args.reasoning_effort or None)
    finally:
        await provider.aclose()
    out = HERE / "results" / args.facts / f"page-{args.character.replace(' ', '_')}-ch{args.at}.md"
    out.write_text(c.text)
    print(f"{len(lines)} facts sent | {c.input_tokens} in / {c.output_tokens} out | {c.served_model}\n")
    print(c.text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True)
    parser.add_argument("--facts", required=True, help="results/<name>/ holding facts.json")
    parser.add_argument("--character", required=True)
    parser.add_argument("--at", type=int, required=True, help="reader's chapter")
    parser.add_argument("--prompt", default="page-v1.txt")
    parser.add_argument("--provider", choices=["book", "deepseek", "local"], default="book")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=900)
    parser.add_argument("--reasoning-effort", default="", help="e.g. none for Qwen on Groq")
    asyncio.run(main(parser.parse_args()))
