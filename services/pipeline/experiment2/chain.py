"""Important plot points across consecutive chapters, linked to the same entities.

Each chapter sees only what earlier chapters introduced (§0: never future text): the
known people, groups, places and items, by ID. The model's one cross-chapter job is to
say "this is E4" even when the chapter spells Aries as Ares. Plot points are never
replaced or edited; a wiki page shows them as a chapter-ordered history, so what was
known at chapter N stays answerable by filtering on the chapter (append-only, §0).

Identity is the model's call from context, never a spelling match (CLAUDE.md: exact
matching *is* the entity-drift bug). Code only assigns IDs and records spellings.

State lives in Postgres, in the tables the reader's wiki already reads: one
`record_generation` per experiment name, one published `record_run` per chapter, and
`entity`/`alias`/`record_row` for what it found. Each chapter commits in one
transaction, so rerunning with the same --name continues after the last published
chapter and a rate-limit failure loses nothing. The generation stays `pending`
(invisible to readers) until --activate points the novel at it.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import asdict

import psycopg
from psycopg.types.json import Jsonb

from facts import HERE, make_provider, read_object
from pipeline.config import Config
from pipeline.llm.provider import Class
from pipeline.provider_config import resolve_provider_config


def context_block(state):
    known = "\n".join(f"{eid}: {' / '.join(e['names'])}"
                      for eid, e in state["entities"].items()) or "(none)"
    return f"KNOWN:\n{known}\n\nCHAPTER:\n"


def apply(state, chapter, text):
    """Parse one response into state. Anything that doesn't fit is kept in `problems`."""
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    added, problems = [], []
    # "+Name" is the model's own label for something new, and it repeats that label on
    # every line of the chapter where the thing first appears. Reusing the label within
    # this one response is not spelling-based identity (CLAUDE.md): across chapters the
    # model must use the ID it is given, and a bare repeat of "+Name" in a later chapter
    # still mints a new entity for review.
    new_labels = {}
    for line in body.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 1)]  # note last: a stray "|" can't shift fields
        if len(parts) != 2 or not parts[1]:
            problems.append({"line": line, "problem": "not 2 fields"})
            continue
        names, note = parts
        refs = []
        for ref in (r.strip() for r in names.split(";")):
            if ref in ("", "-"):  # "-" means no names, not an entity called "-"
                continue
            known = re.fullmatch(r"(E\d+)(?:\s*=\s*(.+))?", ref)
            if known and known[1] in state["entities"]:
                entity = state["entities"][known[1]]
                if known[2] and known[2] not in entity["names"]:
                    entity["names"].append(known[2].strip())  # a spelling, never a new identity
                refs.append(known[1])
            elif known:
                problems.append({"line": line, "problem": f"unknown ID {known[1]}"})
            else:
                # "+Lingfeng=Longfei": one new entity the chapter calls by both names.
                name, *also = [n.strip() for n in ref.lstrip("+").split("=")]
                if name not in new_labels:
                    new_labels[name] = f"E{len(state['entities']) + 1}"
                    state["entities"][new_labels[name]] = {"names": [name], "first_chapter": chapter}
                names = state["entities"][new_labels[name]]["names"]
                names.extend(n for n in also if n and n not in names)
                refs.append(new_labels[name])
        added.append({"entities": refs, "note": note})
    return added, problems


GENERATION_PREFIX = "experiment2:"


def requested_model(args, row, cfg):
    if args.model:
        return args.model
    if args.provider == "deepseek":
        return "deepseek-v4-flash"
    return row.extract_model or row.model or cfg.llm_model_extract


async def open_generation(db, args, model):
    """The experiment's own generation, keyed by --name, created `pending` on first use."""
    tag = f"{GENERATION_PREFIX}{args.name}"
    found = await (await db.execute(
        "SELECT id::text FROM record_generation WHERE novel_id=%s AND prompt_version=%s "
        "AND state <> 'retired' ORDER BY created_at DESC LIMIT 1", (args.novel, tag))).fetchone()
    if found:
        return found[0]
    created = await (await db.execute(
        """INSERT INTO record_generation (novel_id,ontology,prompt_version,checks_version,extraction_model,
                                          source_lang,target_lang,state,predecessor_generation_id)
           SELECT id,ontology,%s,%s,%s,target_lang,target_lang,'pending',active_record_generation
             FROM novel WHERE id=%s RETURNING id::text""",
        (tag, args.prompt, model, args.novel))).fetchone()
    if created is None:
        raise SystemExit(f"no novel {args.novel}")
    return created[0]


async def load_state(db, generation, novel, chapter, source):
    """The KNOWN list for one chapter: glossary names that occur in its source text.

    Code matches locked and pending glossary terms against the Chinese chapter, the same
    exact match TRANSLATE primes with, so the list is bounded by who is in the chapter
    rather than growing with the whole book. A term that is already an entity's Chinese
    alias offers that entity; any other term offers a provisional one, which becomes an
    entity only if the model tags it (publish). Aliases are read only from chapters
    before this one (§0): no identity decided later is shown. Prompt IDs are derived per
    chapter, in order of first appearance, and never stored.
    """
    terms = await (await db.execute(
        """SELECT source_term, target_term FROM glossary
            WHERE novel_id=%s AND NOT deleted AND locked_at_chapter <= %s
           UNION
           SELECT r.source_term, r.candidates->0->>'target_term' FROM character_name_review r
            WHERE r.novel_id=%s AND r.status='pending' AND r.first_seen_chapter <= %s
              AND jsonb_array_length(r.candidates) > 0
              AND NOT EXISTS (SELECT 1 FROM glossary g WHERE g.novel_id=r.novel_id
                              AND g.source_term=r.source_term AND NOT g.deleted)""",
        (novel, chapter, novel, chapter))).fetchall()
    present = sorted((source.index(term), term, target) for term, target in terms
                     if term and target and term in source)
    entities, listed = {}, set()
    for _, term, target in present:
        found = await (await db.execute(
            """SELECT e.id::text, e.first_seen_chapter,
                      array_agg(DISTINCT en.surface) FILTER (WHERE en.surface IS NOT NULL)
                 FROM alias zh JOIN entity e ON e.id=zh.entity_id
                 LEFT JOIN alias en ON en.entity_id=e.id AND en.lang='en' AND en.first_seen_chapter < %s
                WHERE zh.record_generation_id=%s AND zh.lang='zh' AND zh.surface=%s
                  AND zh.first_seen_chapter < %s
                GROUP BY e.id LIMIT 1""",
            (chapter, generation, term, chapter))).fetchone()
        if found and found[0] in listed:
            continue  # a second name for someone already on the list
        label = f"E{len(entities) + 1}"
        if found:
            listed.add(found[0])
            entities[label] = {"uuid": found[0], "names": sorted(found[2] or [target]),
                               "first_chapter": found[1], "zh": []}
        else:
            entities[label] = {"uuid": None, "names": [target], "first_chapter": chapter, "zh": [term]}
    return {"entities": entities}


async def publish(db, args, generation, chapter, source_hash, english, model, completion,
                  state, before, added, problems):
    """Write one chapter's result as a published record_run, all or nothing."""
    request_identity = hashlib.sha256(
        f"{args.prompt}\0{model}\0{context_block(state)}".encode()).hexdigest()
    async with db.transaction():
        run_id = (await (await db.execute(
            """INSERT INTO record_run (novel_id,generation_id,chapter_index,source_hash,display_hash,
                                       request_identity,extraction_model,served_provider,served_model,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'processing') RETURNING id::text""",
            (args.novel, generation, chapter, source_hash, hashlib.sha256(english.encode()).hexdigest(),
             request_identity, model, args.provider, completion.served_model))).fetchone())[0]
        tagged = {label for note in added for label in note["entities"]}
        for label, entity in state["entities"].items():
            created = entity.get("uuid") is None
            if created:
                if label not in tagged:
                    continue  # offered from the glossary but never tagged: no page for it
                entity["uuid"] = (await (await db.execute(
                    """INSERT INTO entity (novel_id,record_generation_id,kind,canonical,canonical_en,first_seen_chapter)
                       VALUES (%s,%s,'unknown',%s,%s,%s) RETURNING id::text""",
                    (args.novel, generation, entity["names"][0], entity["names"][0], chapter))).fetchone())[0]
            # A new spelling is stamped with the chapter that revealed it: an alias can
            # itself be a spoiler, and the reader gate filters on this column. The Chinese
            # name is what later chapters' KNOWN lists find the entity by.
            surfaces = [(name, "en") for name in entity["names"] if created or name not in before.get(label, [])]
            surfaces += [(term, "zh") for term in entity.get("zh", [])] if created else []
            for surface, lang in surfaces:
                await db.execute(
                    """INSERT INTO alias (entity_id,surface,lang,first_seen_chapter,record_generation_id)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (entity["uuid"], surface, lang, chapter, generation))
        for index, note in enumerate(added):
            row_id = (await (await db.execute(
                """INSERT INTO record_row (novel_id,generation_id,run_id,original_index,record_type,
                                           source_chapter,source_hash,valid_from_chapter)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id::text""",
                (args.novel, generation, run_id, index, "FACT", chapter, source_hash,
                 chapter))).fetchone())[0]
            await db.execute("INSERT INTO record_value (row_id,field_name,source_value) VALUES (%s,'note',%s)",
                             (row_id, note["note"]))
            for ordinal, label in enumerate(dict.fromkeys(note["entities"])):
                entity = state["entities"][label]
                await db.execute(
                    """INSERT INTO record_participant (row_id,ordinal,field_name,surface,entity_id)
                       VALUES (%s,%s,'names',%s,%s)""",
                    (row_id, ordinal, entity["names"][0], entity["uuid"]))
        # Only counts reach the database; the problem lines themselves are model text and
        # stay in results/ (freeform text never reaches a read path).
        await db.execute(
            """UPDATE record_run SET status='published', warning_count=%s, diagnostics=%s, published_at=now(),
                   publication_version=COALESCE((SELECT max(publication_version)+1 FROM record_run
                                                  WHERE generation_id=%s),1)
               WHERE id=%s""",
            (len(problems), Jsonb({"notes": len(added), "problems": len(problems)}), generation, run_id))


async def activate(db, novel, generation):
    async with db.transaction():
        previous = (await (await db.execute(
            "SELECT active_record_generation::text FROM novel WHERE id=%s FOR UPDATE", (novel,))).fetchone())[0]
        await db.execute("UPDATE record_generation SET state='active' WHERE id=%s", (generation,))
        await db.execute("UPDATE novel SET active_record_generation=%s WHERE id=%s", (generation, novel))
    print(f"novel now reads generation {generation}; to switch back:\n"
          f"  UPDATE novel SET active_record_generation='{previous}' WHERE id='{novel}';")


async def main(args):
    out = HERE / "results" / args.name
    out.mkdir(parents=True, exist_ok=True)
    system = (HERE / "prompts" / args.prompt).read_text()
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True) as db:
        row = await resolve_provider_config(db, args.novel, cfg.llm_provider)
        model = requested_model(args, row, cfg)
        generation = await open_generation(db, args, model)
        done = (await (await db.execute(
            "SELECT COALESCE(max(chapter_index),0) FROM record_run WHERE generation_id=%s AND status='published'",
            (generation,))).fetchone())[0]
        chapters = {index: (uri, raw_hash, raw_uri) for index, uri, raw_hash, raw_uri in await (await db.execute(
            "SELECT chapter_index, translated_uri, raw_hash, raw_uri FROM chapter WHERE novel_id=%s "
            "AND chapter_index<=%s AND translated_uri IS NOT NULL", (args.novel, args.to))).fetchall()}
        print(f"generation {generation} ({args.name}), chapters 1-{done} already published")
        provider = make_provider(args, row, cfg)
        try:
            for chapter in range(done + 1, args.to + 1):
                if chapter not in chapters:
                    raise SystemExit(f"chapter {chapter} has no English translation yet")
                if args.pause and chapter > done + 1:
                    await asyncio.sleep(args.pause)  # Groq's per-minute output cap
                state = await load_state(db, generation, args.novel, chapter,
                                         read_object(cfg, chapters[chapter][2]))
                english = read_object(cfg, chapters[chapter][0])
                prompt = context_block(state) + english
                completion = await provider.complete(prompt, system=system, cls=Class.BATCH, model=args.model,
                                                     max_output_tokens=args.max_output_tokens,
                                                     reasoning_effort=args.reasoning_effort or None)
                (out / f"ch{chapter}.md").write_text(completion.text)
                before = {label: list(e["names"]) for label, e in state["entities"].items()}
                added, problems = apply(state, chapter, completion.text)
                await publish(db, args, generation, chapter, chapters[chapter][1], english, model,
                              completion, state, before, added, problems)
                with (out / "runs.jsonl").open("a") as log:
                    log.write(json.dumps({"chapter": chapter, "prompt": args.prompt, "provider": args.provider,
                                          "notes": len(added), "problems": problems,
                                          **{k: v for k, v in asdict(completion).items() if k != "text"}},
                                         ensure_ascii=False) + "\n")
                print(f"ch{chapter}: {len(added)} notes, {len(problems)} problems, "
                      f"{completion.input_tokens} in / {completion.output_tokens} out")
        finally:
            await provider.aclose()
        if args.activate:
            await activate(db, args.novel, generation)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", required=True)
    parser.add_argument("--to", type=int, required=True, help="last chapter to process")
    parser.add_argument("--name", required=True,
                        help="experiment name: its record_generation and results/<name>/ (resumes if it exists)")
    parser.add_argument("--prompt", default="important-v11.txt")
    parser.add_argument("--provider", choices=["book", "deepseek"], default="book")
    parser.add_argument("--model", help="defaults: book's model / deepseek-v4-flash")
    parser.add_argument("--max-output-tokens", type=int, default=1000)
    parser.add_argument("--pause", type=int, default=65, help="seconds between calls (0 for providers without a per-minute cap)")
    parser.add_argument("--reasoning-effort", default="low",
                        help="low keeps DeepSeek's thinking from running past the output cap; '' for the provider default")
    parser.add_argument("--activate", action="store_true",
                        help="afterwards, point the novel's reader at this generation")
    asyncio.run(main(parser.parse_args()))
