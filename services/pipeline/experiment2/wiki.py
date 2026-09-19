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


_CHAPTER_REF = r"ch\.?\s*\d+(?:\s*[–-]\s*\d+)?"
_CITATION = re.compile(rf"\(\s*{_CHAPTER_REF}(?:\s*[,;]\s*(?:{_CHAPTER_REF}|\d+(?:\s*[–-]\s*\d+)?))*\s*\)")


def tidy_citations(page: str) -> str:
    """One citation style: "(ch4, ch5)" and "(ch. 3-4)" become "(ch. 4, 5)" and "(ch. 3–4)"."""
    def one(match: re.Match) -> str:
        parts = re.findall(r"\d+(?:\s*[–-]\s*\d+)?", match.group(0))
        return "(ch. " + ", ".join(re.sub(r"\s*[–-]\s*", "–", part) for part in parts) + ")"
    return _CITATION.sub(one, page)


def checked_aliases(page: str, name: str, facts: list[tuple[int, str]]) -> str:
    """Keep an "Also known as" name only if a fact from the chapter it cites names both it
    and this character. Titles the model made up, or read into the wrong fact, go."""
    def says(chapter: int, alias: str) -> bool:
        return any(c == chapter and alias.lower() in text.lower() and name.lower() in text.lower()
                   for c, text in facts)

    def one(match: re.Match) -> str:
        kept = []
        for item in re.split(r"[;,](?![^()]*\))", match.group(1)):
            found = re.match(r"\s*(.+?)\s*\(ch\.\s*(\d+)[^)]*\)\s*$", item)
            if found and says(int(found.group(2)), found.group(1).strip("\"' ")):
                kept.append(f"{found.group(1).strip()} (ch. {found.group(2)})")
        return f"Also known as: {', '.join(kept)}\n" if kept else ""
    return re.sub(r"^Also known as:(.*)\n?", one, page, count=1, flags=re.M)


async def write_page(provider, args, name: str, facts: list[tuple[int, str]]):
    prompt = f"CHARACTER: {name}\n\nFACTS (chapters 1-{args.at}):\n" + "\n".join(
        f"[ch{chapter}] {text}" for chapter, text in facts)
    completion = await provider.complete(
        prompt, system=(HERE / "prompts" / args.prompt).read_text(), cls=Class.BATCH,
        model=args.model, max_output_tokens=args.max_output_tokens,
        **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}))
    page = checked_aliases(tidy_citations(completion.text), name, facts)
    out = HERE / "results" / f"wiki-{args.novel[:8]}-ch{args.at}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name.replace(' ', '_')}-{args.prompt.removesuffix('.txt')}.md").write_text(page)
    print(f"--- {name}: {len(facts)} facts sent | {completion.input_tokens} in / "
          f"{completion.output_tokens} out | {completion.served_model}\n")
    print(page, "\n")


async def people_with_spellings(db, novel: str, at: int) -> dict[str, str]:
    """Source term -> current spelling for the people met by chapter `at`: a live glossary
    lock first, then the reader's selection, then the pending choice."""
    rows = await (await db.execute(
        """SELECT r.source_term, COALESCE(g.target_term, r.selected_target, r.candidates->0->>'target_term')
             FROM character_name_review r
             LEFT JOIN glossary g ON g.novel_id=r.novel_id AND g.source_term=r.source_term AND NOT g.deleted
            WHERE r.novel_id=%s AND r.first_seen_chapter <= %s AND r.term_role = ANY(%s)""",
        (novel, at, ["chinese_person", "foreign_person"]))).fetchall()
    return {term: spelling for term, spelling in rows if spelling}


def named_in(text: str, spellings: dict[str, str]) -> list[str]:
    """Source terms whose spelling names someone in `text`, leftmost-longest: a spelling
    inside a longer known one is not a separate mention."""
    found = []
    for term, spelling in sorted(spellings.items(), key=lambda item: -len(item[1])):
        pattern = re.compile(rf"(?<![\w-]){re.escape(spelling)}(?![\w-])")
        if pattern.search(text):
            found.append(term)
            text = pattern.sub(" ", text)
    return found


async def build(args):
    """Update each named character's page chapter by chapter, storing every version."""
    version = args.prompt
    system = (HERE / "prompts" / version).read_text()
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        facts = await load_facts(db, args.novel, args.to)
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        provider = None if args.dry else make_provider(args, row, cfg)
        spent_in = spent_out = 0
        try:
            for chapter in sorted({c for c, _ in facts}):
                spellings = await people_with_spellings(db, args.novel, chapter)
                about: dict[str, list[str]] = {}
                for c, text in facts:
                    if c == chapter:
                        for term in named_in(text, spellings):
                            about.setdefault(term, []).append(text)
                for term, lines in sorted(about.items(), key=lambda item: -len(item[1])):
                    title = spellings[term]
                    done = await (await db.execute(
                        "SELECT 1 FROM wiki_page WHERE novel_id=%s AND prompt_version=%s AND subject=%s AND chapter_index=%s",
                        (args.novel, version, term, chapter))).fetchone()
                    if done:
                        continue
                    if args.dry:
                        print(f"ch{chapter}: {title} ({len(lines)} facts)")
                        continue
                    previous = await (await db.execute(
                        """SELECT body FROM wiki_page WHERE novel_id=%s AND prompt_version=%s AND subject=%s
                             AND chapter_index < %s ORDER BY chapter_index DESC LIMIT 1""",
                        (args.novel, version, term, chapter))).fetchone()
                    prompt = (f"CHARACTER: {title}\n\nCURRENT PAGE:\n{previous[0] if previous else '(none yet)'}"
                              f"\n\nNEW FACTS:\n" + "\n".join(f"[ch{chapter}] {line}" for line in lines))
                    completion = await provider.complete(
                        prompt, system=system, cls=Class.BATCH, model=args.model,
                        max_output_tokens=args.max_output_tokens,
                        **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}))
                    page = checked_aliases(tidy_citations(completion.text),
                                           title, [f for f in facts if f[0] <= chapter])
                    await db.execute(
                        """INSERT INTO wiki_page (novel_id, subject, chapter_index, prompt_version, title, body,
                                                  facts_used, served_model)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (args.novel, term, chapter, version, title, page, len(lines), completion.served_model))
                    spent_in += completion.input_tokens
                    spent_out += completion.output_tokens
                    print(f"ch{chapter}: {title} ({len(lines)} facts) {completion.input_tokens} in / "
                          f"{completion.output_tokens} out")
        finally:
            if provider:
                await provider.aclose()
    if not args.dry:
        print(f"total {spent_in} in / {spent_out} out")


async def main(args):
    if args.build:
        return await build(args)
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
    parser.add_argument("--at", type=int, help="the reader's chapter (--list/--character/--top)")
    parser.add_argument("--to", type=int, help="--build: last chapter to build through")
    parser.add_argument("--dry", action="store_true", help="--build: show the plan, no model calls")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--build", action="store_true",
                     help="update pages chapter by chapter into wiki_page (use --prompt wiki-update-v1.txt)")
    who.add_argument("--list", action="store_true", help="rank the people met by fact count")
    who.add_argument("--character")
    who.add_argument("--top", type=int, help="write pages for the N most-mentioned people")
    parser.add_argument("--prompt", default="wiki-page-v4.txt")
    parser.add_argument("--provider", choices=["book", "deepseek", "local"], default="book")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--reasoning-effort", default="none",
                        help="DeepSeek: none keeps a page from spending its budget on thinking")
    asyncio.run(main(parser.parse_args()))
