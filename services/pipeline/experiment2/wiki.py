"""Write wiki pages from a book's stored facts, as a reader at chapter --at would see them.

Reads `chapter_fact` (the FACTS stage's output) for chapters <= --at only, so a page is
never written from text the reader hasn't reached (§0). No name list is sent: facts
already use the book's spellings, so an alias ("also known as") can only come from a
fact, and every fact carries its chapter. A glossary hint would leak later names, because
a confirmed spelling is locked at chapter 0.

    wiki.py --novel ID --at 10 --list               # who the reader has met, by fact count
    wiki.py --novel ID --at 10 --character "Ling Feng"
    wiki.py --novel ID --at 10 --top 5              # pages for the five most-mentioned

Writes results/wiki-<novel>-ch<at>/<Name>.md and prints each page. Nothing is written
to Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import re

import psycopg

from facts import HERE, make_provider
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import resolve_provider_config

PEOPLE = ("chinese_person", "foreign_person", "personal_title")


async def load_facts(db, novel: str, at: int) -> list[tuple[int, str]]:
    """Each chapter's newest prompt version, chapters <= at, in story order."""
    rows = await (await db.execute(
        """SELECT f.chapter_index, f.text FROM chapter_fact f
            WHERE f.novel_id=%s AND f.chapter_index <= %s
              AND f.prompt_version = (SELECT max(prompt_version) FROM chapter_fact g
                                       WHERE g.novel_id=f.novel_id AND g.chapter_index=f.chapter_index)
            ORDER BY f.chapter_index, f.ordinal""", (novel, at))).fetchall()
    return [(chapter, text) for chapter, text in rows]


async def people_met(db, novel: str, at: int) -> list[str]:
    """Spellings of the people a reader at chapter `at` has met (first seen <= at)."""
    rows = await (await db.execute(
        """SELECT COALESCE(selected_target, candidates->0->>'target_term') FROM character_name_review
            WHERE novel_id=%s AND first_seen_chapter <= %s AND term_role = ANY(%s)""",
        (novel, at, list(PEOPLE)))).fetchall()
    return sorted({name for (name,) in rows if name})


def mentions(name: str, facts: list[tuple[int, str]], people: list[str]) -> int:
    """Facts that name this person, not counting a longer name containing it ("Feng"
    inside "Ling Feng"): leftmost-longest, like the scanner."""
    def whole(term: str) -> re.Pattern:
        return re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])")
    longer = [whole(other) for other in people if other != name and whole(name).search(other)]
    pattern = whole(name)
    count = 0
    for _, text in facts:
        for other in longer:
            text = other.sub(" ", text)
        count += bool(pattern.search(text))
    return count


async def write_page(provider, args, name: str, facts: list[tuple[int, str]]):
    prompt = f"CHARACTER: {name}\n\nFACTS (chapters 1-{args.at}):\n" + "\n".join(
        f"[ch{chapter}] {text}" for chapter, text in facts)
    completion = await provider.complete(
        prompt, system=(HERE / "prompts" / args.prompt).read_text(), cls=Class.BATCH,
        model=args.model, max_output_tokens=args.max_output_tokens,
        **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}))
    out = HERE / "results" / f"wiki-{args.novel[:8]}-ch{args.at}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name.replace(' ', '_')}-{args.prompt.removesuffix('.txt')}.md").write_text(completion.text)
    print(f"--- {name}: {len(facts)} facts sent | {completion.input_tokens} in / "
          f"{completion.output_tokens} out | {completion.served_model}\n")
    print(completion.text, "\n")


async def main(args):
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url) as db:
        facts = await load_facts(db, args.novel, args.at)
        people = await people_met(db, args.novel, args.at)
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
    ranked = sorted(((mentions(name, facts, people), name) for name in people), reverse=True)
    if args.list:
        print(f"{len(facts)} facts in chapters 1-{args.at}")
        for count, name in ranked:
            if count:
                print(f"{count:4}  {name}")
        return
    names = [args.character] if args.character else [name for count, name in ranked[:args.top] if count]
    provider = make_provider(args, row, cfg)
    try:
        for name in names:
            await write_page(provider, args, name, facts)
    finally:
        await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True)
    parser.add_argument("--at", type=int, required=True, help="the reader's chapter")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--list", action="store_true", help="rank the people met by fact count")
    who.add_argument("--character")
    who.add_argument("--top", type=int, help="write pages for the N most-mentioned people")
    parser.add_argument("--prompt", default="wiki-page-v1.txt")
    parser.add_argument("--provider", choices=["book", "deepseek", "local"], default="book")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--reasoning-effort", default="none",
                        help="DeepSeek: none keeps a page from spending its budget on thinking")
    asyncio.run(main(parser.parse_args()))
