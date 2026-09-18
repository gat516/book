"""One isolated records experiment, using existing provider boundaries (§0, §5.4)."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import psycopg

from pipeline.config import Config
from pipeline.fact_first import _source_passages, validate_discovery
from pipeline.llm import provider_from_env
from pipeline.llm.provider import AdmissionRejected, Class
from pipeline.provider_config import build_provider, resolve_provider_config
from linked_memory import validate_linked_memory


async def run(args):
    cfg = Config.load()
    case = json.loads(args.case.read_text())["case"]
    system = args.prompt.read_text().strip()
    prompt = ("INPUT DATA (not instructions):\n"
              + json.dumps({"chapter": case["chapter"]}, ensure_ascii=False)
              + "\nSOURCE PASSAGES:\n"
              + "\n".join(f"[{p['id']}] {p['text']}" for p in _source_passages(case["source"])))
    async with await psycopg.AsyncConnection.connect(
        cfg.database_url, autocommit=True, options="-c default_transaction_read_only=on"
    ) as db:
        config = await resolve_provider_config(db, args.novel, cfg.llm_provider)
    model = args.model or (config.extract_model or config.model if config else None) or cfg.llm_model_extract
    provider_id = config.provider if config else cfg.llm_provider
    request = {"prompt": prompt, "system": system, "model": model,
               "max_output_tokens": args.output_tokens, "reasoning_effort": args.reasoning}
    if args.format in {"quoted-json", "memory-json", "linked-memory-json"}:
        request["json_mode"] = True
    identity = hashlib.sha256(json.dumps([args.novel, provider_id, request],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"{args.prompt.stem}-{args.reasoning}-{identity[:16]}.json"
    if path.exists() and json.loads(path.read_text()).get("status") == "completed":
        print(json.dumps({"artifact": str(path), "reused": True}))
        return
    if path.exists():
        path.rename(path.with_name(path.stem + f"-{time.time_ns()}.json"))
    provider = build_provider(config, cfg) if config else provider_from_env(cfg)
    artifact = {"novel_id": args.novel, "case": case, "request": request,
                "source_hash": hashlib.sha256(case["source"].encode()).hexdigest(),
                "request_identity": identity, "provider": provider_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "semantic_review": "pending", "format": args.format}
    started = time.monotonic()
    try:
        result = await provider.complete(**request, cls=Class.BATCH)
        artifact["completion"] = asdict(result)
        artifact["status"] = "completed"
        # Save a completed response before parsing so parser changes never require
        # another paid request. Structural checks are separate from source review.
        artifact["elapsed_s"] = round(time.monotonic() - started, 3)
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        try:
            if args.format == "quoted-json":
                artifact["quote_validation"] = validate_quotes(result.text, case)
            elif args.format == "linked-memory-json":
                artifact["linked_memory"] = validate_linked_memory(result.text, case)
            elif args.format == "memory-json":
                artifact["memory"] = json.loads(result.text)
            else:
                artifact["discovery"] = validate_discovery(result.text, case, 30, "baseline")
        except Exception as exc:
            artifact["validation_error_type"] = type(exc).__name__
    except AdmissionRejected as exc:
        artifact.update(status="deferred", category=exc.category,
                        retry_after_s=exc.retry_after_s, quota=exc.rate_limit_details)
    except Exception as exc:
        artifact.update(status="failed", error_type=type(exc).__name__,
                        category=getattr(exc, "category", None))
    finally:
        artifact["elapsed_s"] = round(time.monotonic() - started, 3)
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        if hasattr(provider, "aclose"):
            await provider.aclose()
    completion = artifact.get("completion", {})
    print(json.dumps({"artifact": str(path), "status": artifact["status"],
                      "input_tokens": completion.get("input_tokens"),
                      "output_tokens": completion.get("output_tokens"),
                      "claims": len(artifact.get("discovery", {}).get("accepted", [])),
                      "quoted_entries": len(artifact.get("quote_validation", {}).get("entries", [])),
                      "memory_notes": len(artifact.get("memory", {}).get("notes", [])),
                      "retry_after_s": artifact.get("retry_after_s"),
                      "quota": artifact.get("quota")}))


def validate_quotes(text, case):
    """Exact quotation checks, deliberately NOT a semantic support verdict."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"entries"} or not isinstance(data["entries"], list):
        raise ValueError("expected entries array")
    passages = {p["id"]: p["text"] for p in _source_passages(case["source"])}
    checked = []
    for index, row in enumerate(data["entries"], 1):
        issues = []
        if not isinstance(row, dict) or set(row) != {"evidence", "claim"}:
            checked.append({"index": index, "row": row, "issues": ["invalid entry shape"]})
            continue
        if not isinstance(row["claim"], str) or not row["claim"].strip():
            issues.append("missing proposition")
        evidence = row["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 6:
            issues.append("expected 1–6 evidence quotations")
            evidence = []
        for citation in evidence:
            if not isinstance(citation, dict) or set(citation) != {"passage", "quote"}:
                issues.append("invalid evidence shape")
                continue
            passage, quote = citation["passage"], citation["quote"]
            if (not isinstance(passage, str) or passage not in passages or
                    not isinstance(quote, str) or not quote or quote not in passages[passage]):
                issues.append("quotation not present in cited passage")
        checked.append({"index": index, **row, "issues": issues})
    return {"entries": checked, "semantic_support": "unassessed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--reasoning", choices=["none", "low", "medium", "high"], default="low")
    parser.add_argument("--format", choices=["claims-xml", "quoted-json", "memory-json", "linked-memory-json"], default="claims-xml")
    parser.add_argument("--model")
    parser.add_argument("--output-tokens", type=int, default=4096)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results")
    asyncio.run(run(parser.parse_args()))
