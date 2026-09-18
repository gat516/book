"""Facts only: one call per chapter, no tags, IDs or KNOWN list.

Each fact is a short story summary kept out of the reader's view; wiki pages are meant
to be written later from these by a separate summary step. Reads the English translation
of chapters 1..--to (never later text, §0) and writes results/<name>/chN.md plus
facts.json. Nothing is written to Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re

import psycopg

from facts import HERE, make_provider, read_object
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import resolve_provider_config


def parse(text):
    """Lines after the last `## ` heading, with any bullets or numbering stripped."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    return [line for line in (re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
                              for raw in body.splitlines()) if line]


async def main(args):
    out = HERE / "results" / args.name
    out.mkdir(parents=True, exist_ok=True)
    system = (HERE / "prompts" / args.prompt).read_text()
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url) as db:
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        uris = dict(await (await db.execute(
            "SELECT chapter_index, translated_uri FROM chapter WHERE novel_id=%s AND chapter_index<=%s "
            "AND translated_uri IS NOT NULL", (args.novel, args.to))).fetchall())
    provider = make_provider(args, row, cfg)
    results = {}
    try:
        for chapter in range(1, args.to + 1):
            if chapter > 1 and args.pause:
                await asyncio.sleep(args.pause)  # Groq's per-minute caps
            completion = await provider.complete(read_object(cfg, uris[chapter]), system=system,
                                                 cls=Class.BATCH, model=args.model,
                                                 max_output_tokens=args.max_output_tokens)
            (out / f"ch{chapter}.md").write_text(completion.text)
            results[chapter] = {"facts": parse(completion.text), "served_model": completion.served_model,
                                "input_tokens": completion.input_tokens,
                                "output_tokens": completion.output_tokens}
            print(f"ch{chapter}: {len(results[chapter]['facts'])} facts, "
                  f"{completion.input_tokens} in / {completion.output_tokens} out", flush=True)
    finally:
        await provider.aclose()
        (out / "facts.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True)
    parser.add_argument("--to", type=int, required=True)
    parser.add_argument("--name", required=True, help="results/<name>/")
    parser.add_argument("--prompt", default="story-facts-v1.txt")
    parser.add_argument("--provider", choices=["book", "deepseek", "local"], default="book")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=950)
    parser.add_argument("--pause", type=int, default=65)
    asyncio.run(main(parser.parse_args()))
