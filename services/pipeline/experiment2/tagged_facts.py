"""Facts tagged with a wiki category, one call per chapter. Writes nothing to Postgres.

Each fact line is `category | fact` or `relationship: kind | fact`; code, not the model,
later decides whose page a fact belongs on (exact name match), and assembles each page
section from the facts carrying its tag. Reads the English translation of the chosen
chapters only and saves results/<name>/tagged.json.

    tagged_facts.py --novel ID --chapters 1 2 7 --name tagged-v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter

import psycopg

from facts import HERE, make_provider, read_object
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import resolve_provider_config

CATEGORIES = {"intro", "alias", "relationship", "ability", "item", "affiliation", "status", "event"}
# Relationship kinds a page shows in its own section; any other kind the model names
# is kept and shown with the rest of the generated material.
DISPLAYED_KINDS = {"friend", "enemy", "lover", "family", "superior", "subordinate"}
# "<Name> is <Name>'s <kind>": who holds the role, and whose it is.
RELATION = re.compile(r"^(.+?) is (.+?)['’]s (\w+)")
LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*([a-z]+)(?:\s*:\s*([a-z][a-z -]*?))?\s*\|\s*(.+?)\s*$", re.I)


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Tagged facts after the last `## ` heading, and the lines that didn't fit the format."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    facts, rejected = [], []
    for raw in body.splitlines():
        if not raw.strip():
            continue
        match = LINE.match(raw)
        category = match and match.group(1).lower()
        kind = match and (match.group(2) or "").lower()
        if not match or category not in CATEGORIES or (category == "relationship" and not kind):
            rejected.append(raw.strip())
            continue
        facts.append({"category": category, "kind": kind or None, "fact": match.group(3)})
    return facts, rejected


async def main(args):
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url) as db:
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        uris = dict(await (await db.execute(
            "SELECT chapter_index, translated_uri FROM chapter WHERE novel_id=%s AND chapter_index = ANY(%s)",
            (args.novel, args.chapters))).fetchall())
    provider = make_provider(args, row, cfg)
    system = (HERE / "prompts" / args.prompt).read_text()
    out, totals = {}, Counter()
    try:
        for chapter in args.chapters:
            completion = await provider.complete(
                read_object(cfg, uris[chapter]), system=system, cls=Class.BATCH, model=args.model,
                max_output_tokens=args.max_output_tokens, reasoning_effort=args.reasoning_effort)
            facts, rejected = parse(completion.text)
            out[chapter] = {"facts": facts, "rejected": rejected, "output_tokens": completion.output_tokens}
            tags = Counter(f"{f['category']}:{f['kind']}" if f["kind"] else f["category"] for f in facts)
            totals.update(tags)
            print(f"ch{chapter}: {len(facts)} facts, {len(rejected)} unparsed | out={completion.output_tokens} | "
                  + ", ".join(f"{tag} {n}" for tag, n in tags.most_common()))
            for fact in facts:
                label = f"{fact['category']}:{fact['kind']}" if fact["kind"] else fact["category"]
                print(f"   {label:24} {fact['fact']}")
                if fact["category"] == "relationship":
                    pair = RELATION.match(fact["fact"])
                    print("      -> " + (f"{pair.group(1)} is {pair.group(2)}'s {pair.group(3)}"
                                         if pair else "direction not readable"))
            for line in rejected:
                print(f"   UNPARSED  {line}")
    finally:
        await provider.aclose()
    result = HERE / "results" / args.name
    result.mkdir(parents=True, exist_ok=True)
    (result / "tagged.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print("all chapters: " + ", ".join(f"{tag} {n}" for tag, n in totals.most_common()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapters", type=int, nargs="+", required=True)
    parser.add_argument("--name", required=True, help="results/<name>/")
    parser.add_argument("--prompt", default="tagged-facts-v3.txt")
    parser.add_argument("--provider", choices=["book", "deepseek", "local"], default="book")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=16000)
    parser.add_argument("--reasoning-effort", default="low", help="matches the FACTS stage")
    asyncio.run(main(parser.parse_args()))
