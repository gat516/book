"""Incremental identity and budget-sized rendering of saved notes; never publish (§0)."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

import psycopg

from compact_downstream_trial import check_links, decode_local
from downstream_trial import LocalCalls, save
from pipeline.config import Config
from pipeline.fact_first import _source_passages
from pipeline.llm.provider import AdmissionRejected
from pipeline.name_renderings import conventional_english_names
from pipeline.provider_config import build_provider, resolve_provider_config
from pipeline.term_choices import provisional_plan

PROMPTS = Path(__file__).parent / "prompts"
MARKER = re.compile(r"⟦(t[0-9]+)⟧")


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(text):
    """Planning heuristic, not billed usage or a model-specific tokenizer."""
    non_ascii = sum(ord(c) > 127 for c in text)
    return math.ceil(non_ascii * 1.2 + (len(text) - non_ascii) / 3)


def evidence_for(notes, full_passages, *, neighbors=False):
    cited = {p for n in notes.values() for p in n["passages"]}
    wanted = set(cited)
    if not wanted <= full_passages.keys():
        raise ValueError("note cites unknown source passage")
    ordered = list(full_passages)
    # Citations are never truncated to save tokens. Speaker context is retained
    # before quoted dialogue; the bounded unresolved pass can expand both sides.
    for i, pid in enumerate(ordered):
        if pid not in cited:
            continue
        if neighbors or full_passages[pid].lstrip().startswith(('“', '「', '『', '"')):
            if i:
                wanted.add(ordered[i - 1])
    if neighbors:
        wanted = {ordered[j] for i, pid in enumerate(ordered) if pid in cited
                  for j in range(max(0, i - 1), min(len(ordered), i + 2))}
    return {p: text for p, text in full_passages.items() if p in wanted}


def retrieve_candidates(notes, passages, registry, limit=10):
    """Rank possibilities only; spelling similarity never creates a link (§5)."""
    context = "\n".join([n["note"] for n in notes.values()] + list(passages.values()))
    context_bigrams = {context[i:i + 2] for i in range(len(context) - 1)}
    def score(entity):
        exact = sum(len(n) for n in entity["names"] if n in context)
        partial = sum(len({n[i:i + 2] for i in range(len(n) - 1)} & context_bigrams)
                      for n in entity["names"])
        return exact * 10 + partial
    ranked = sorted(enumerate(registry.values()), key=lambda pair: (-score(pair[1]), -pair[0]))
    # Retain a few contextual alternatives even with no lexical match; the model
    # can request more through unresolved rather than inventing a missing match.
    return [e for _, e in ranked[:limit]]


def identity_payload(notes, full_passages, ontology, registry, prefix, candidate_limit,
                     *, neighbors=False):
    passages = evidence_for(notes, full_passages, neighbors=neighbors)
    candidates = retrieve_candidates(notes, passages, registry, candidate_limit)
    payload = {"kinds": ontology["kinds"], "notes": notes, "passages": passages,
               "prefix": prefix, "candidates": [[e["id"], e["kind"], e["names"]] for e in candidates]}
    if ontology.get("kind_descriptions"):
        payload["kind_descriptions"] = ontology["kind_descriptions"]
    return payload, {e["id"]: e for e in candidates}


def choose_identity_batch(items, full_passages, ontology, registry, prefix, args, system):
    batch, chosen = {}, None
    for nid, note in items:
        trial = {**batch, nid: note}
        payload, candidates = identity_payload(trial, full_passages, ontology, registry,
                                                prefix, args.candidate_limit)
        input_estimate = estimate_tokens(system + wire(payload))
        # Note size and unresolved surface density proxy output work. Existing
        # candidate references are cheap; new definitions need evidence and names.
        note_size = estimate_tokens(" ".join(n["note"] for n in trial.values()))
        output_estimate = 90 + note_size * 2.7 + len(trial) * 30
        if batch and (input_estimate > args.input_budget or
                      output_estimate > args.link_output_tokens * .8):
            break
        if input_estimate > args.input_budget:
            raise ValueError("one note exceeds identity input budget; evidence retained, no call made")
        batch, chosen = trial, (payload, candidates, input_estimate, math.ceil(output_estimate))
    return batch, chosen


def validate_delta(raw, notes, passages, kinds, candidates, prefix):
    if not isinstance(raw, dict) or set(raw) != {"new", "aliases", "links"}:
        raise ValueError("expected new, aliases and links arrays")
    if any(not isinstance(raw[k], list) for k in raw):
        raise ValueError("delta fields must be arrays")
    entities = []
    for row in raw["new"]:
        # Accept the equivalent ordered-name wire shape without regenerating a
        # paid response. All names and witnesses still undergo the same checks.
        if (isinstance(row, list) and len(row) == 4 and isinstance(row[2], list)
                and row[2] and all(isinstance(n, str) for n in row[2])):
            row = [row[0], row[1], row[2][0], row[2][1:], row[3]]
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("invalid new entity row")
        eid, kind, canonical, aliases, pids = row
        if (not isinstance(eid, str) or not eid.startswith(prefix) or eid in candidates
                or not isinstance(canonical, str) or not canonical
                or not isinstance(aliases, list) or not isinstance(pids, list)):
            raise ValueError("invalid new entity ID or names")
        entities.append({"id": eid, "kind": kind, "names": [canonical, *aliases],
                         "passages": pids, "candidate": None})
    links, diagnostics = [], []
    for row in raw["links"]:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("invalid delta link row")
        nid, ids, unresolved = row
        if isinstance(unresolved, str):
            unresolved = [unresolved] if unresolved.strip() else []
        if (not isinstance(nid, str) or not isinstance(ids, list)
                or any(not isinstance(e, str) for e in ids)):
            raise ValueError("invalid note ID or links")
        if len(ids) != len(set(ids)):
            diagnostics.append({"note": nid, "action": "deduplicate identical linked IDs"})
        # Removing the same ID twice is set normalization, not identity merging.
        links.append({"note": nid, "entities": list(dict.fromkeys(ids)), "unresolved": unresolved})
    checked = check_links({"entities": entities, "links": links}, notes, passages,
                          kinds, existing_ids=candidates)
    checked["diagnostics"].extend(diagnostics)
    for link in checked["links"]:
        invalid = [ref for ref in link["unresolved"]
                   if not ref.strip() or ref in passages or ref in candidates
                   or not any(ref in text for text in passages.values())]
        if invalid:
            # Preserve the original uncertainty, but explicitly request a full-note
            # review rather than treating a passage ID as a name or guessing one.
            checked["diagnostics"].append({"note": link["note"],
                "action": "review note: unresolved references lack source witnesses",
                "invalid_references": invalid})
    alias_deltas, seen = [], set()
    for row in raw["aliases"]:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("invalid alias delta")
        eid, names, pids = row
        if (not isinstance(eid, str) or eid not in candidates or eid in seen
                or not isinstance(names, list) or not names
                or any(not isinstance(n, str) or not n for n in names)
                or not isinstance(pids, list) or not pids
                or any(not isinstance(p, str) or p not in passages for p in pids)):
            raise ValueError("alias delta requires a supplied candidate and source evidence")
        seen.add(eid)
        grounded = [n for n in names if n not in candidates[eid]["names"]
                    and any(n in passages[p] for p in pids)]
        if set(names) - set(grounded) - set(candidates[eid]["names"]):
            checked["diagnostics"].append({"entity": eid, "action": "omit ungrounded alias delta"})
        if grounded:
            alias_deltas.append((eid, grounded, pids))
    # No canonical/type rewrites, ID merges, or lookup-by-spelling occurs here.
    return checked, alias_deltas


def apply_delta(registry, checked, aliases):
    for entity in checked["entities"]:
        if entity["id"] in registry:
            raise ValueError("new ID collides with registry")
        registry[entity["id"]] = entity
    for eid, names, pids in aliases:
        prior = registry[eid]
        registry[eid] = {**prior, "names": list(dict.fromkeys(prior["names"] + names)),
                         "passages": list(dict.fromkeys(prior["passages"] + pids))}


async def link_notes(args, calls, notes, passages, ontology, summary):
    registry, links, diagnostics = {}, {}, []
    system = (PROMPTS / "link-memory-delta-v1.txt").read_text()
    pending = list(notes.items())
    batch_number = 0
    while pending:
        batch_number += 1
        prefix = f"b{batch_number}e"
        batch, (payload, candidates, estimated_input, estimated_output) = choose_identity_batch(
            pending, passages, ontology, registry, prefix, args, system)
        result = await calls.call(f"link_delta_{batch_number}", request(args, system, payload,
                                                                        args.link_output_tokens))
        checked, aliases = validate_delta(decode_local(result["text"]), batch, payload["passages"],
                                          ontology["kinds"], candidates, prefix)
        apply_delta(registry, checked, aliases)
        links.update({row["note"]: row for row in checked["links"]})
        diagnostics.extend(checked["diagnostics"])
        summary.setdefault("batches", []).append({"stage": "identity", "notes": list(batch),
            "candidate_count": len(candidates), "passage_count": len(payload["passages"]),
            "estimated_input": estimated_input, "estimated_output": estimated_output})
        pending = pending[len(batch):]
        summary["linked"] = {"entities": list(registry.values()), "links": list(links.values()),
                             "diagnostics": diagnostics}
        save(args.output / "summary.json", summary)

    # At most one additional evidence/candidate expansion per unresolved note. It
    # cannot silently change already accepted links or merge existing IDs (§0).
    repairs = 0
    for nid, link in list(links.items()):
        if not link["unresolved"] or repairs >= args.identity_followups:
            continue
        batch = {nid: notes[nid]}
        prefix = f"r{repairs + 1}e"
        payload, candidates = identity_payload(batch, passages, ontology, registry, prefix,
                                                args.candidate_limit * 2, neighbors=True)
        payload["unresolved"] = link["unresolved"]
        payload["accepted_links"] = link["entities"]
        payload["review_instruction"] = (
            "Review all named participants of this note. Earlier unresolved entries may be malformed; "
            "replace them with exact source names, resolve witnessed new entities, and retain uncertainty "
            "when evidence is insufficient. Accepted links are retained by code.")
        if estimate_tokens(system + wire(payload)) > args.input_budget:
            diagnostics.append({"note": nid, "action": "leave unresolved: follow-up exceeds budget"})
            continue
        repairs += 1
        result = await calls.call(f"identity_followup_{nid}", request(args, system, payload,
                                                                      args.link_output_tokens))
        checked, aliases = validate_delta(decode_local(result["text"]), batch, payload["passages"],
                                          ontology["kinds"], candidates, prefix)
        apply_delta(registry, checked, aliases)
        repaired = checked["links"][0]
        links[nid] = {**repaired, "entities": list(dict.fromkeys(link["entities"] + repaired["entities"]))}
        diagnostics.extend(checked["diagnostics"])
    return {"entities": list(registry.values()), "links": [links[n] for n in notes],
            "diagnostics": diagnostics}


def request(args, system, payload, output_tokens):
    return {"system": system, "prompt": wire(payload), "model": args.model,
            "max_output_tokens": output_tokens, "reasoning_effort": "none", "json_mode": False}


def mark_terms(notes, linked, glossary):
    """Terminology markers are surface references, never entity bindings (§0)."""
    text = "\n".join(n["note"] for n in notes.values())
    if MARKER.search(text):
        raise ValueError("source notes contain reserved rendering markers")
    surfaces = set(glossary) | {n for e in linked["entities"] for n in e["names"]}
    surfaces = sorted((s for s in surfaces if s and s in text), key=lambda s: (-len(s), s))
    terms = {f"t{i}": s for i, s in enumerate(surfaces, 1)}
    ids = {s: tid for tid, s in terms.items()}
    pattern = re.compile("|".join(re.escape(s) for s in surfaces)) if surfaces else None
    marked = {nid: pattern.sub(lambda m: f"⟦{ids[m[0]]}⟧", n["note"]) if pattern else n["note"]
              for nid, n in notes.items()}
    spellings = {}
    for tid, surface in terms.items():
        known = conventional_english_names(surface)
        if surface in glossary:
            spellings[tid] = glossary[surface]
        elif known:
            spellings[tid] = known[0]
    return marked, terms, spellings


def choose_render_batch(items, terms, spellings, args, system):
    batch, chosen = {}, None
    for nid, note in items:
        trial = {**batch, nid: note}
        used = set(MARKER.findall("\n".join(trial.values())))
        payload = {"notes": trial, "new_terms": {tid: terms[tid] for tid in terms
                                                  if tid in used and tid not in spellings}}
        # English rendering plus one proposal per new term; leave output headroom.
        expected = estimate_tokens(" ".join(trial.values())) * 1.1 + len(payload["new_terms"]) * 22 + 40
        if batch and (expected > args.render_output_tokens * .8 or
                      estimate_tokens(system + wire(payload)) > args.input_budget):
            break
        if estimate_tokens(system + wire(payload)) > args.input_budget:
            raise ValueError("one note exceeds rendering input budget")
        batch, chosen = trial, payload
    return batch, chosen


def accept_rendering(raw, payload, terms, spellings):
    if (not isinstance(raw, dict) or set(raw) != {"notes", "terms"}
            or not isinstance(raw["notes"], dict) or set(raw["notes"]) != set(payload["notes"])
            or not isinstance(raw["terms"], dict) or set(raw["terms"]) != set(payload["new_terms"])):
        raise ValueError("rendering omitted or added note/term IDs")
    choices = dict(spellings)
    for tid, proposal in raw["terms"].items():
        if (not isinstance(proposal, list) or len(proposal) != 2
                or any(not isinstance(x, str) or not x.strip() for x in proposal)
                or MARKER.search(proposal[0])):
            raise ValueError("invalid provisional term proposal")
        target, role = proposal
        plan = provisional_plan(terms[tid], target, role, "en")
        if plan is None or not plan.candidates:
            raise ValueError("unsupported provisional term role or spelling")
        choices[tid] = plan.candidates[0].target_term
    translations = {}
    for nid, source in payload["notes"].items():
        translated = raw["notes"][nid]
        if (not isinstance(translated, str) or not translated.strip()
                or Counter(MARKER.findall(translated)) != Counter(MARKER.findall(source))):
            raise ValueError("rendering changed terminology markers")
        translations[nid] = MARKER.sub(lambda m: choices[m[1]], translated)
    # Validate every note before accepting any naming choice; no DB glossary writes.
    spellings.update(choices)
    return translations


def usage_report(calls, extraction):
    def total(attempts):
        return {k: sum(a["completion"].get(k, 0) for a in attempts)
                for k in ("input_tokens", "output_tokens")}
    successful = total(calls.attempts)
    extraction_usage = {k: extraction["completion"].get(k, 0) for k in successful}
    return {"downstream_usage": successful, "extraction_usage": extraction_usage,
            "total_usage": {k: successful[k] + extraction_usage[k] for k in successful},
            "new_successful_usage": total([a for a in calls.attempts if not a["reused"]]),
            "reused_successful_usage": total([a for a in calls.attempts if a["reused"]]),
            "unknown_usage_attempts": sum(e["status"] != "completed" for e in calls.events)}


async def run(args):
    extraction = json.loads(args.extraction.read_text())
    case = extraction["case"]
    if case["chapter"] != 1:
        raise ValueError("Chapter 1 only; earlier-chapter candidates are not implemented")
    if hashlib.sha256(case["source"].encode()).hexdigest() != extraction["source_hash"]:
        raise ValueError("saved extraction source hash mismatch")
    notes = {f"n{i}": n for i, n in enumerate(extraction["memory"]["notes"], 1)}
    passages = {p["id"]: p["text"] for p in _source_passages(case["source"])}
    evidence_for(notes, passages)
    cfg = Config.load()
    # §0.3: even local experiments retrieve only knowledge available at this chapter.
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True,
            connect_timeout=10, options="-c default_transaction_read_only=on") as db:
        config = await resolve_provider_config(db, extraction["novel_id"], cfg.llm_provider)
        glossary = dict(await (await db.execute(
            "SELECT source_term,target_term FROM glossary WHERE novel_id=%s AND locked_at_chapter<=%s AND NOT deleted ORDER BY source_term",
            (extraction["novel_id"], case["chapter"]))).fetchall())
    if config is None:
        raise ValueError("saved book provider configuration is required")
    args.output.mkdir(parents=True, exist_ok=True)
    # §6.1: isolate replay by provider as well as model, source, and visible glossary.
    manifest = {"version": 1, "novel": extraction["novel_id"], "source_hash": extraction["source_hash"],
                "provider": config.provider, "model": args.model,
                "endpoint_hash": hashlib.sha256((config.base_url or "").encode()).hexdigest(),
                "glossary_hash": hashlib.sha256(wire(glossary).encode()).hexdigest()}
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("output directory belongs to different inputs; choose a new directory")
    save(manifest_path, manifest)
    provider = build_provider(config, cfg)
    calls = LocalCalls(provider, args.output, args.admission_retries, max_new_calls=args.max_new_calls,
                       min_interval=args.min_request_interval)
    summary = {"status": "started", "published": False, "source_chapter": case["chapter"],
               "source_hash": extraction["source_hash"], "extraction_artifact": str(args.extraction),
               "representation": "grouped_notes_with_entity_links",
               "stages": {"identity": "pending", "render": "pending"},
               "limitations": ["Chapter 1 only; no cross-chapter identity validation.",
                               "Experimental grouped notes, not production typed-record storage.",
                               "Token estimates plan batches; only provider usage is measured.",
                               "Failed/deferred attempts may have unreported usage."]}
    try:
        summary["stages"]["identity"] = "processing"
        linked = await link_notes(args, calls, notes, passages, case["ontology"], summary)
        summary["linked"] = linked
        summary["stages"]["identity"] = "completed"
        summary["stages"]["render"] = "processing"
        marked, terms, spellings = mark_terms(notes, linked, glossary)
        translations, pending = {}, list(marked.items())
        system = (PROMPTS / "render-memory-markers-v1.txt").read_text()
        number = 0
        while pending:
            number += 1
            batch, payload = choose_render_batch(pending, terms, spellings, args, system)
            result = await calls.call(f"render_markers_{number}", request(args, system, payload,
                                                                          args.render_output_tokens))
            translations.update(accept_rendering(decode_local(result["text"]), payload, terms, spellings))
            pending = pending[len(batch):]
            summary.setdefault("batches", []).append({"stage": "render", "notes": list(batch),
                                                        "new_terms": len(payload["new_terms"])})
        summary["naming_map"] = {terms[t]: s for t, s in spellings.items()}
        summary["records"] = [{"id": n, "source_chapter": case["chapter"], "source_text": notes[n]["note"],
                               "target_text": translations[n], "evidence_ids": notes[n]["passages"],
                               "entities": link["entities"], "unresolved": link["unresolved"]}
                              for link in linked["links"] for n in [link["note"]]]
        summary["status"] = "completed_local_stages"
        summary["stages"]["render"] = "completed"
        (args.output / "reader-preview.md").write_text("# Experimental reader notes\n\n" + "\n\n".join(
            f"{i}. {r['target_text']}\n\n   Evidence: {', '.join(r['evidence_ids'])}; unresolved: {wire(r['unresolved'])}"
            for i, r in enumerate(summary["records"], 1)) + "\n")
    except AdmissionRejected as exc:
        summary.update(status="deferred", retry_after_s=exc.retry_after_s, quota=exc.rate_limit_details)
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, category=getattr(exc, "category", None))
        if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
            summary["validation_error"] = str(exc)
    finally:
        for stage, status in summary["stages"].items():
            if status == "processing":
                summary["stages"][stage] = summary["status"]
        if summary["stages"]["render"] == "pending":
            summary["render_blocked_by"] = "identity did not finish; see validation_error or retry_after_s"
        summary.update(usage_report(calls, extraction), attempts=calls.attempts, events=calls.events)
        save(args.output / "summary.json", summary)
        await provider.aclose()
        print(wire({k: summary.get(k) for k in ("status", "total_usage", "new_successful_usage",
                   "unknown_usage_attempts", "validation_error", "retry_after_s")}), flush=True)
    return summary["status"] == "completed_local_stages"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="qwen/qwen3.8-27b")
    parser.add_argument("--input-budget", type=int, default=2400)
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--link-output-tokens", type=int, default=900)
    parser.add_argument("--render-output-tokens", type=int, default=900)
    parser.add_argument("--identity-followups", type=int, default=1)
    parser.add_argument("--max-new-calls", type=int, default=10)
    parser.add_argument("--admission-retries", type=int, default=0)
    parser.add_argument("--min-request-interval", type=float, default=0)
    args = parser.parse_args()
    if min(args.input_budget, args.candidate_limit, args.link_output_tokens,
           args.render_output_tokens, args.max_new_calls) <= 0 or min(args.identity_followups, args.admission_retries, args.min_request_interval) < 0:
        parser.error("budgets must be positive; retry/follow-up counts must be nonnegative")
    return args


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run(parse_args())) else 1)
