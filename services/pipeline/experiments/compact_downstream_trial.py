"""Experimental entity-linked notes: identity pass, then English rendering; local only."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import psycopg

from downstream_trial import LocalCalls, save
from pipeline.config import Config
from pipeline.fact_first import _source_passages
from pipeline.llm.provider import AdmissionRejected
from pipeline.provider_config import build_provider, resolve_provider_config
from pipeline.stages.records import _decode_rendering


def decode_local(text):
    """Strip a Markdown wrapper only; never complete or alter JSON content."""
    body = text.strip()
    if body.startswith("```json\n") or body.startswith("```\n"):
        body = body.split("\n", 1)[1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].rstrip()
    return _decode_rendering(body)


def check_links(data, notes, passages, kinds, existing_ids=()):
    """Check shape, source spelling and referential integrity, not semantic truth."""
    if not isinstance(data, dict) or set(data) != {"entities", "links"}:
        raise ValueError("expected entities and links")
    if not isinstance(data["entities"], list) or not isinstance(data["links"], list):
        raise ValueError("expected arrays")
    ids, declared = set(existing_ids), set()
    accepted, dropped, diagnostics = [], {}, []
    for entity in data["entities"]:
        if set(entity) != {"id", "kind", "names", "passages", "candidate"}:
            raise ValueError("invalid entity fields")
        eid = entity["id"]
        if not isinstance(eid, str) or not eid or eid in declared:
            raise ValueError("invalid or duplicate entity ID")
        declared.add(eid)
        ids.add(eid)
        if entity["kind"] not in kinds or entity["candidate"] is not None:
            raise ValueError("unknown kind or prior candidate")
        names, citations = entity["names"], entity["passages"]
        if not isinstance(names, list) or not names or not isinstance(citations, list) or not citations:
            raise ValueError("missing names or citations")
        if any(not isinstance(p, str) for p in citations):
            raise ValueError("invalid entity passage")
        unknown = [p for p in citations if p not in passages]
        citations = [p for p in citations if p in passages]
        if unknown:
            diagnostics.append({"entity": eid, "unknown_passages": unknown, "action": "omit unsupplied citations"})
        if any(not isinstance(n, str) or not n for n in names):
            raise ValueError("invalid entity name")
        grounded = [n for n in names if any(n in passages[p] for p in citations)]
        if grounded != names:
            diagnostics.append({"entity": eid, "unanchored_names": [n for n in names if n not in grounded],
                                "action": "omit unanchored alias" if grounded else "leave identity unresolved"})
        if grounded:
            accepted.append({**entity, "names": grounded, "passages": citations})
        else:
            dropped[eid] = names
    seen = set()
    for link in data["links"]:
        if set(link) != {"note", "entities", "unresolved"}:
            raise ValueError("invalid link fields")
        nid = link["note"]
        if nid not in notes or nid in seen:
            raise ValueError("unknown or duplicate note link")
        seen.add(nid)
        if not isinstance(link["entities"], list) or any(e not in ids for e in link["entities"]):
            raise ValueError("unknown entity link")
        if not isinstance(link["unresolved"], list) or any(not isinstance(x, str) for x in link["unresolved"]):
            raise ValueError("invalid unresolved references")
    if seen != set(notes):
        raise ValueError("missing note links")
    # §0: one failed identity witness must not discard unrelated chapter notes.
    # This never guesses a replacement identity or merges by source spelling.
    links = [{**link, "entities": [e for e in link["entities"] if e not in dropped],
              "unresolved": list(dict.fromkeys(link["unresolved"] +
                                [n for e in link["entities"] for n in dropped.get(e, [])]))}
             for link in data["links"]]
    return {"entities": accepted, "links": links, "diagnostics": diagnostics}


async def run(args):
    extraction = json.loads(args.extraction.read_text())
    case = extraction["case"]
    if case["chapter"] != 1:
        raise ValueError("First-chapter trial only; no prior candidate registry yet")
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True,
            options="-c default_transaction_read_only=on") as db:
        config = await resolve_provider_config(db, extraction["novel_id"], cfg.llm_provider)
        glossary = await (await db.execute("SELECT source_term,target_term FROM glossary WHERE novel_id=%s AND locked_at_chapter<=%s AND NOT deleted ORDER BY source_term",
                                           (extraction["novel_id"], case["chapter"]))).fetchall()
    assert config is not None
    notes = {f"n{i}": note for i, note in enumerate(extraction["memory"]["notes"], 1)}
    full_passages = {p["id"]: p["text"] for p in _source_passages(case["source"])}
    cited = {p for n in notes.values() for p in n["passages"]}
    passages = {p: full_passages[p] for p in sorted(cited)}
    args.output.mkdir(parents=True, exist_ok=True)
    provider = build_provider(config, cfg)
    calls = LocalCalls(provider, args.output, args.admission_retries)
    prompt_dir = Path(__file__).parent / "prompts"
    summary = {"status": "started", "extraction_artifact": str(args.extraction),
               "representation": "grouped_notes_with_entity_links", "source_chapter": case["chapter"],
               "source_hash": extraction["source_hash"], "published": False,
               "usage_scope": "Successful request usage only; failed provider calls may have consumed unreported tokens.",
               "limitations": ["Not the existing atomic fact/relation/event storage contract.",
                               "No database publication or reader API exercised.",
                               "Chapter 1 has no prior identity candidates; cross-chapter reuse is untested."]}
    try:
        if args.notes_per_call:
            linked = await link_batches(args, calls, notes, full_passages, case["ontology"], summary)
        else:
            linked = await link_once(args, calls, notes, passages, case["ontology"])
        summary["linked"] = linked
        save(args.output / "summary.json", summary)
        note_text = "\n".join(n["note"] for n in notes.values())
        source_text = "\n".join(passages.values())
        useful_glossary = {s: t for s, t in glossary if s in note_text or s in source_text}
        translations = {}
        render_items = list(notes.items())
        render_size = args.render_notes_per_call or len(render_items)
        for offset in range(0, len(render_items), render_size):
            batch = dict(render_items[offset:offset+render_size])
            rendered = await calls.call(f"render_batch_{offset // render_size + 1}", {
                "system": (prompt_dir / "render-memory-v1.txt").read_text(),
                "prompt": json.dumps({"notes": {k: v["note"] for k, v in batch.items()}, "glossary": useful_glossary},
                                     ensure_ascii=False, separators=(",", ":")),
                "model": args.model, "max_output_tokens": args.render_output_tokens,
                "reasoning_effort": "none", "json_mode": not args.plain_output})
            decoded = decode_local(rendered["text"])
            if set(decoded) != set(batch):
                raise ValueError("rendering omitted or added batch note IDs")
            translations.update(decoded)
        if not isinstance(translations, dict) or set(translations) != set(notes) or any(not isinstance(x, str) or not x.strip() for x in translations.values()):
            raise ValueError("rendering omitted or added note IDs")
        summary["records"] = [{"id": link["note"], "source_chapter": case["chapter"],
                               "source_text": notes[link["note"]]["note"],
                               "target_text": translations[link["note"]],
                               "evidence_ids": notes[link["note"]]["passages"],
                               "entities": link["entities"], "unresolved": link["unresolved"]}
                              for link in linked["links"]]
        summary["status"] = "completed_local_stages"
        summary["glossary"] = useful_glossary
    except AdmissionRejected as exc:
        summary.update(status="deferred", retry_after_s=exc.retry_after_s, quota=exc.rate_limit_details)
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, category=getattr(exc, "category", None))
        # Structural check messages are local constants, not provider exception text.
        if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
            summary["validation_error"] = str(exc)
    finally:
        summary["attempts"] = calls.attempts
        summary["downstream_usage"] = {k: sum(a["completion"].get(k, 0) for a in calls.attempts)
                                       for k in ("input_tokens", "output_tokens")}
        summary["extraction_usage"] = {k: extraction["completion"].get(k, 0) for k in ("input_tokens", "output_tokens")}
        summary["total_usage"] = {k: summary["downstream_usage"][k] + summary["extraction_usage"][k]
                                  for k in ("input_tokens", "output_tokens")}
        save(args.output / "summary.json", summary)
        await provider.aclose()
        print(json.dumps({k: summary.get(k) for k in ["status", "downstream_usage", "total_usage", "validation_error", "retry_after_s"]}), flush=True)


async def link_once(args, calls, notes, passages, ontology):
        payload = {"ontology": ontology, "notes": notes, "passages": passages, "prior_candidates": []}
        result = await calls.call("link", {"system": args.link_prompt.read_text(),
                                           "prompt": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                           "model": args.model, "max_output_tokens": args.link_output_tokens,
                                           "reasoning_effort": args.link_reasoning, "json_mode": not args.plain_output})
        raw_linked = decode_local(result["text"])
        if args.compact_wire:
            explicit = all(isinstance(e, list) and len(e) == 4 for e in raw_linked["entities"])
            entity_rows = raw_linked["entities"] if explicit else [[f"e{i}", *e] for i, e in enumerate(raw_linked["entities"])]
            raw_linked = {"entities": [{"id": eid, "kind": kind, "names": names,
                                        "passages": pids, "candidate": None}
                                       for eid, kind, names, pids in entity_rows],
                          "links": [{"note": nid, "entities": ids if explicit else [f"e{i}" for i in ids], "unresolved": refs}
                                    for nid, ids, refs in raw_linked["links"]]}
        return check_links(raw_linked, notes, passages, ontology["kinds"])


async def link_batches(args, calls, notes, full_passages, ontology, summary):
    registry, links, diagnostics = {}, [], []
    ordered = list(full_passages)
    positions = {p: i for i, p in enumerate(ordered)}
    items = list(notes.items())
    for offset in range(0, len(items), args.notes_per_call):
        number = offset // args.notes_per_call + 1
        batch = dict(items[offset:offset + args.notes_per_call])
        # Current-chapter neighbors supply speaker context; no future chapter is read.
        wanted = {ordered[j] for note in batch.values() for p in note["passages"]
                  for j in range(max(0, positions[p]-1), min(len(ordered), positions[p]+2))}
        passages = {p: full_passages[p] for p in ordered if p in wanted}
        prefix = f"b{number}e"
        payload = {"ontology": ontology, "notes": batch, "passages": passages,
                   "new_id_prefix": prefix,
                   "existing_entities": [{k: e[k] for k in ("id", "kind", "names")} for e in registry.values()]}
        result = await calls.call(f"link_batch_{number}", {
            "system": args.link_prompt.read_text(),
            "prompt": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "model": args.model, "max_output_tokens": args.link_output_tokens,
            "reasoning_effort": args.link_reasoning, "json_mode": not args.plain_output})
        raw = decode_local(result["text"])
        entities = [{"id": eid, "kind": kind, "names": [canonical, *aliases],
                     "passages": pids, "candidate": None}
                    for eid, kind, canonical, aliases, pids in raw["entities"]]
        for e in entities:
            if e["id"] not in registry and not e["id"].startswith(prefix):
                raise ValueError("new entity uses an unknown batch prefix")
            if e["id"] in registry and e["kind"] != registry[e["id"]]["kind"]:
                raise ValueError("existing entity kind changed")
        checked = check_links({"entities": entities,
                               "links": [{"note": n, "entities": ids, "unresolved": [] if refs == "" else refs} for n, ids, refs in raw["links"]]},
                              batch, passages, ontology["kinds"], existing_ids=registry)
        for e in checked["entities"]:
            prior = registry.get(e["id"])
            # Only the model's explicit existing ID authorizes reuse; code never
            # looks up identity by matching a name. Retain prior witnesses as well.
            registry[e["id"]] = e if prior is None else {**e,
                "names": list(dict.fromkeys(prior["names"] + e["names"])),
                "passages": list(dict.fromkeys(prior["passages"] + e["passages"]))}
        links.extend(checked["links"])
        diagnostics.extend({"batch": number, **d} for d in checked["diagnostics"])
        summary["linked"] = {"entities": list(registry.values()), "links": links, "diagnostics": diagnostics}
        save(args.output / "summary.json", summary)
    return {"entities": list(registry.values()), "links": links, "diagnostics": diagnostics}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen/qwen3.8-27b")
    parser.add_argument("--link-reasoning", choices=["none", "low", "medium"], default="none")
    parser.add_argument("--link-prompt", type=Path, default=Path(__file__).parent / "prompts/link-memory-v2.txt")
    parser.add_argument("--compact-wire", action="store_true")
    parser.add_argument("--link-output-tokens", type=int, default=4096)
    parser.add_argument("--plain-output", action="store_true")
    parser.add_argument("--notes-per-call", type=int, default=0)
    parser.add_argument("--render-notes-per-call", type=int, default=0)
    parser.add_argument("--render-output-tokens", type=int, default=4096)
    parser.add_argument("--admission-retries", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
