"""Hosted evidence-first experiment: extract once; enrich/resolve selected records offline."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import psycopg

from evidence_jobs import EvidenceJobs, JobStopped, freeze_json, write_json
from evidence_format import REPAIR_SYSTEM, parse_lines, repair_payload, merge_repairs
from evidence_memory import (
    CHECKS_VERSION, ENRICHMENT_VERSION, HERE, decode, digest, earlier_entities, enrichment_payload, estimate_tokens,
    extraction_request, gated_records, policy_for, resolution_payload, retrieve, search_source,
    validate_enrichment, validate_resolution, validate_selection, wire,
)
from pipeline.config import Config
from pipeline.provider_config import build_provider, resolve_provider_config


def load_case(path):
    saved = json.loads(path.read_text())
    case = saved["case"]
    if (not isinstance(saved.get("novel_id"), str) or not saved["novel_id"]
            or type(case.get("chapter")) is not int or case["chapter"] <= 0
            or not isinstance(case.get("source"), str) or not case["source"].strip()
            or not isinstance(case.get("ontology", {}).get("kinds"), list)
            or not case["ontology"]["kinds"]
            or any(not isinstance(k, str) or not k for k in case["ontology"]["kinds"])):
        raise ValueError("case requires novel_id, positive chapter, source, and ontology kinds")
    source_hash = hashlib.sha256(case["source"].encode()).hexdigest()
    if saved.get("source_hash", source_hash) != source_hash:
        raise ValueError("case source hash mismatch")
    # Explicit allowlist prevents accidental propagation of an old experiment's raw responses.
    return {"novel_id": saved["novel_id"], "source_hash": source_hash,
            "case": {"chapter": case["chapter"], "source": case["source"], "ontology": case["ontology"]}}


def make_generation(policy, ontology, identity, model, reasoning, output_tokens):
    return digest({"policy": policy, "ontology": ontology, "provider": identity,
                   "model": model, "reasoning": reasoning, "output_tokens": output_tokens,
                   "system": (HERE / "prompts/evidence-select-v1.txt").read_text(), "checks": CHECKS_VERSION,
                   "repair_system": REPAIR_SYSTEM, "repair_limits": [1600, 900]})


async def connection(novel_id):
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True,
            connect_timeout=10, options="-c default_transaction_read_only=on") as db:
        row = await resolve_provider_config(db, novel_id, cfg.llm_provider)
    if row is None or row.provider == "ollama":
        raise ValueError("this experiment uses the book's existing hosted provider configuration")
    return cfg, row


async def glossary_for(cfg, novel_id, chapter):
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True,
            connect_timeout=10, options="-c default_transaction_read_only=on") as db:
        rows = await (await db.execute(
            "SELECT source_term,target_term FROM glossary WHERE novel_id=%s AND locked_at_chapter<=%s AND NOT deleted ORDER BY source_term",
            (novel_id, chapter))).fetchall()
    return dict(rows)


def preview(artifact, drafts=()):
    notes = {rid: text for draft in drafts for rid, text in draft.get("notes", {}).items()}
    lines = [f"# Evidence review — chapter {artifact['source_chapter']}", "",
             "Experimental evidence selections. Semantic review pending; nothing published.", ""]
    for row in artifact["result"]["records"]:
        lines.extend([f"## {row['id']} · {row['topic']}", ""])
        if row["id"] in notes:
            lines.extend(["Draft English note: " + notes[row["id"]], ""])
        for p in row["evidence"]:
            lines.extend([f"Source passage {p['id']}:", "", *("> " + line for line in p["text"].splitlines()), ""])
        if row["unresolved"]:
            lines.extend(["Unresolved: " + ", ".join(row["unresolved"]), ""])
    return "\n".join(lines) + "\n"


async def extract(args, jobs, provider_factory, identity):
    saved = load_case(args.case)
    policy = policy_for(json.loads(args.policy.read_text()) if args.policy else None)
    generation = make_generation(policy, saved["case"]["ontology"], identity, args.model,
                                 args.reasoning, args.output_tokens)
    earlier = earlier_entities([load_artifact(p) for p in args.prior], novel_id=saved["novel_id"],
                               chapter=saved["case"]["chapter"], generation=generation)
    candidates = retrieve(saved["case"]["source"], earlier, policy["candidate_limit"])
    request = extraction_request(saved["case"], policy, candidates, model=args.model,
                                 reasoning=args.reasoning, output_tokens=args.output_tokens)
    context = {"novel_id": saved["novel_id"], "source_chapter": saved["case"]["chapter"],
               "source_hash": saved["source_hash"], "generation": generation}
    freeze_json(args.output / "extraction-input.json", {**saved, **context, "request": request, "policy": policy})
    completion, key = await jobs.call("extract", request, context, provider_factory)
    parsed, broken = parse_lines(completion["text"], saved["case"], policy, candidates)
    # Persist the usable first response before any optional repair (§0.7).
    freeze_json(args.output / "parsed-extraction.json", {"accepted": parsed, "broken": broken})
    repair_path = args.output / "extraction-repair.json"
    if repair_path.exists():
        repair = json.loads(repair_path.read_text())
        parsed, broken = repair["accepted"], repair["remaining"]
    else:
        repair = {"status": "not_needed", "accepted": parsed, "remaining": broken}
        if broken:
            selected = []
            for item in broken:
                payload = repair_payload([*selected, item], parsed, saved["case"], policy, candidates)
                if estimate_tokens(REPAIR_SYSTEM + wire(payload)) <= 1600:
                    selected.append(item)
            repair["status"] = "input_budget_exceeded"
            if selected:
                payload = repair_payload(selected, parsed, saved["case"], policy, candidates)
                repair_request = {**request, "system": REPAIR_SYSTEM, "prompt": wire(payload),
                                  "max_output_tokens": 900, "json_mode": False}
                try:
                    fixed, repair_key = await jobs.call("extract-repair", repair_request, context, provider_factory)
                    parsed, remaining = merge_repairs(fixed["text"], parsed, selected, saved["case"], policy, candidates)
                    selected_slots = {s["slot"] for s in selected}
                    broken = [b for b in broken if b["slot"] not in selected_slots] + remaining
                    repair.update(status="completed", request_key=repair_key, completion=fixed)
                except Exception as exc:
                    # One bounded attempt; retain valid entries even if repair fails.
                    repair.update(status="unavailable", error_type=type(exc).__name__)
            repair.update(accepted=parsed, remaining=broken)
        freeze_json(repair_path, repair)
    result = validate_selection(wire(parsed), saved["case"], policy, candidates, digest(context))
    result["diagnostics"].extend({"where": f"line {b['slot']}", "action": "leave malformed entry unused",
                                  "error": b["error"]} for b in broken)
    artifact = {**saved, **context, "format": "evidence-memory-v1", "policy": policy,
                "provider_identity": identity, "request_key": key, "request": request,
                "checks_version": CHECKS_VERSION, "completion": completion, "candidates": candidates,
                "result": result, "repair": repair, "published": False}
    artifact["artifact_hash"] = digest(artifact)
    freeze_json(args.output / "evidence.json", artifact)
    (args.output / "evidence-preview.md").write_text(preview(artifact))
    return {"status": "evidence_ready_for_review", "records": len(result["records"]),
            "identities": len(result["entities"]), "unresolved_references": sum(len(r["unresolved"]) for r in result["records"]),
            "diagnostics": len(result["diagnostics"]), "artifact": str(args.output / "evidence.json")}


def load_artifact(path):
    artifact = json.loads(path.read_text())
    checksum = artifact.pop("artifact_hash", None)
    if artifact.get("format") != "evidence-memory-v1" or checksum != digest(artifact):
        raise ValueError("invalid or changed immutable evidence artifact")
    artifact["artifact_hash"] = checksum
    return artifact


def cached_names(jobs, artifact, at, glossary):
    """Only locally validated drafts from this evidence/cap can supply provisional terminology."""
    names = {}
    for path in sorted((jobs.root / "drafts").glob("*.json")):
        draft = json.loads(path.read_text())
        if (draft.get("artifact_hash") == artifact["artifact_hash"] and draft.get("at") == at
                and draft.get("kind") == "enrich"):
            for source, target in draft["result"]["names"].items():
                names.setdefault(source, target)
    names.update(glossary)  # approved spellings always win
    return names


async def enrich(args, jobs, provider_factory, artifact, glossary):
    selected = gated_records(artifact, args.records, args.at)
    context = {"novel_id": artifact["novel_id"], "source_chapter": artifact["source_chapter"],
               "artifact_hash": artifact["artifact_hash"], "at": args.at, "kind": args.command}
    if args.command == "enrich":
        context["enrichment_version"] = ENRICHMENT_VERSION
    if args.command == "resolve" and not args.reason:
        raise ValueError("identity follow-up needs an explicit reader-feature or conflict reason")
    system = (HERE / f"prompts/evidence-{'enrich' if args.command == 'enrich' else 'resolve'}-v1.txt").read_text()
    # Freeze the initial naming map for this selection. Otherwise the first run's
    # own new spellings would change its request on resume and bypass its cache.
    naming_key = digest([context, args.records, args.model, args.reasoning, args.output_tokens, system, glossary])
    naming_path = args.output / "naming-inputs" / f"{naming_key}.json"
    if naming_path.exists():
        naming = json.loads(naming_path.read_text())
    else:
        naming = cached_names(jobs, artifact, args.at, glossary)
        freeze_json(naming_path, naming)
    completed = []
    # Each optional job is keyed by its explicit record selection. English jobs
    # pack only requested evidence; later batches consume the shared naming map.
    while selected:
        if args.command == "resolve":
            batch = selected
            payload = resolution_payload(batch, artifact, artifact["policy"]["candidate_limit"])
        else:
            batch = []
            for record in selected:
                # Optional context must not force extra completions (§0.7). Pack
                # original evidence first, then use only the remaining allowance.
                proposed = enrichment_payload([*batch, record], artifact, naming, include_context=False)
                if batch and estimate_tokens(system + wire(proposed)) > artifact["policy"]["max_enrichment_input_tokens"]:
                    break
                batch.append(record)
            payload = enrichment_payload(batch, artifact, naming,
                                         input_token_budget=artifact["policy"]["max_enrichment_input_tokens"]
                                         - estimate_tokens(system))
        if estimate_tokens(system + wire(payload)) > artifact["policy"]["max_enrichment_input_tokens"]:
            raise JobStopped("selected evidence exceeds enrichment input budget; no source was truncated")
        request = {"system": system, "prompt": wire(payload), "model": args.model,
                   "reasoning_effort": args.reasoning, "max_output_tokens": args.output_tokens, "json_mode": True}
        completion, key = await jobs.call(args.command, request, context, provider_factory)
        result = (validate_enrichment(completion["text"], payload) if args.command == "enrich"
                  else validate_resolution(completion["text"], payload))
        draft = {**context, "request_key": key, "result": result,
                 "served_provider": completion["served_provider"], "served_model": completion["served_model"],
                 "reason": getattr(args, "reason", None)}
        freeze_json(args.output / "drafts" / f"{key}.json", draft)
        completed.append(draft)
        if args.command == "enrich":
            for source, target in result["names"].items():
                naming.setdefault(source, target)
        selected = selected[len(batch):]
    if args.command == "enrich":
        (args.output / "enrichment-preview.md").write_text(preview(artifact, [d["result"] for d in completed]))
    return {"status": "drafts_ready_for_review", "jobs": len(completed),
            "records": args.records, "published": False}


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    artifact = load_artifact(args.output / "evidence.json") if args.command != "extract" else None
    if args.command == "search":
        print(wire({"matches": search_source(artifact, args.query, args.at, args.limit), "provider_calls": 0}), flush=True)
        return True
    saved = load_case(args.case) if args.command == "extract" else artifact
    if args.command != "extract":
        gated_records(artifact, args.records, args.at)  # gate BEFORE provider/DB reads
    if args.replay_only:
        identity = json.loads((args.output / "provider.json").read_text())
        provider_factory = lambda: (_ for _ in ()).throw(JobStopped("offline replay cannot call a provider"))
        cfg = None
    else:
        cfg, row = await connection(saved["novel_id"])
        identity = {"provider": row.provider, "endpoint_hash": digest(row.base_url or "")}
        provider_factory = lambda: build_provider(row, cfg)
    if artifact is not None and identity != artifact["provider_identity"]:
        raise ValueError("provider changed; use a new experiment generation")
    with EvidenceJobs(args.output, identity, max_attempts=args.max_attempts, interval=args.min_request_interval,
                      retry_failed=args.retry_failed, replay_only=args.replay_only) as jobs:
        outcome = {"status": "started", "command": args.command}
        try:
            if args.command == "extract":
                outcome.update(await extract(args, jobs, provider_factory, identity))
            else:
                # Freeze visible terminology once. Replay is independent of DB/network.
                glossary_path = args.output / f"glossary-{args.at}.json"
                if glossary_path.exists():
                    glossary = json.loads(glossary_path.read_text())
                elif args.replay_only:
                    raise JobStopped("offline replay has no saved glossary snapshot")
                else:
                    glossary = await glossary_for(cfg, artifact["novel_id"], min(args.at, artifact["source_chapter"]))
                    freeze_json(glossary_path, glossary)
                outcome.update(await enrich(args, jobs, provider_factory, artifact, glossary))
        except Exception as exc:
            outcome.update(status="stopped" if isinstance(exc, JobStopped) else "failed",
                           error_type=type(exc).__name__, category=getattr(exc, "category", None))
            if isinstance(exc, JobStopped) or type(exc) is ValueError:
                outcome["detail"] = str(exc)  # local constants only
        finally:
            outcome["usage"] = jobs.report()
            write_json(args.output / "last-run.json", outcome)
            print(wire(outcome), flush=True)
        return outcome["status"] in {"evidence_ready_for_review", "drafts_ready_for_review"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Search all saved chapter source locally, without model calls")
    search.add_argument("--output", required=True, type=Path)
    search.add_argument("--query", required=True)
    search.add_argument("--at", required=True, type=int)
    search.add_argument("--limit", default=10, type=int)
    for command in ("extract", "enrich", "resolve"):
        sub = commands.add_parser(command)
        sub.add_argument("--output", required=True, type=Path)
        sub.add_argument("--model", default="qwen/qwen3.8-27b")
        sub.add_argument("--reasoning", choices=["none", "low", "medium", "high"], default="none")
        sub.add_argument("--output-tokens", type=int, default=3000 if command == "extract" else 900)
        sub.add_argument("--max-attempts", type=int, default=2 if command == "extract" else 1,
                         help="Absolute attempt ceiling for this directory, including prior runs/failures")
        sub.add_argument("--min-request-interval", type=float, default=60)
        sub.add_argument("--retry-failed", action="store_true")
        sub.add_argument("--replay-only", action="store_true")
        if command == "extract":
            sub.add_argument("--case", required=True, type=Path)
            sub.add_argument("--policy", type=Path)
            sub.add_argument("--prior", action="append", type=Path, default=[])
        else:
            sub.add_argument("--records", nargs="+", required=True)
            sub.add_argument("--at", type=int, required=True)
        if command == "resolve":
            sub.add_argument("--reason", required=True, choices=["reader-feature", "identity-conflict"])
    args = parser.parse_args()
    if args.command == "search":
        if args.limit <= 0:
            parser.error("result limit must be positive")
        return args
    if args.max_attempts < 0 or args.output_tokens <= 0 or args.min_request_interval < 0:
        parser.error("invalid attempt, token, or pacing budget")
    return args


if __name__ == "__main__":
    try:
        success = asyncio.run(run(parse_args()))
    except Exception as exc:
        print(wire({"status": "failed", "error_type": type(exc).__name__}), flush=True)
        success = False
    raise SystemExit(0 if success else 1)
