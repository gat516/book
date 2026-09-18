"""Exercise existing downstream stages locally; never publish or enqueue work."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
import time

import psycopg

from pipeline.config import Config
from pipeline.fact_first import _source_passages, normalization_input, normalization_request, validate_extraction
from pipeline.llm.provider import AdmissionRejected, Class
from pipeline.provider_config import build_provider, resolve_provider_config
from pipeline.stages.records import RecordsStage


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class LocalCalls:
    """Checkpoint real provider responses without the production queue or cache."""
    def __init__(self, provider, output, admission_retries=0):
        self.provider, self.output, self.attempts = provider, output, []
        self.admission_retries = admission_retries

    async def call(self, stage, request):
        for attempt in range(self.admission_retries + 1):
            try:
                return await self._call_once(stage, request)
            except AdmissionRejected as exc:
                delay = exc.retry_after_s
                if (attempt == self.admission_retries or exc.category != "rate_limited"
                        or delay is None or delay > 120):
                    raise
                delay = max(float(delay), 60.0 if not exc.exact_hint else 1.0)
                print(json.dumps({"stage": stage, "waiting_seconds": delay,
                                  "admission_retry": attempt + 1}), flush=True)
                while delay > 0:
                    interval = min(delay, 60)
                    await asyncio.sleep(interval)
                    delay -= interval

    async def _call_once(self, stage, request):
        request = {k: v for k, v in request.items() if k != "stage" and v is not None}
        key = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.output / f"{stage}-{key[:16]}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get("status") == "completed":
                self.attempts.append({"stage": stage, "artifact": str(path), "completion": saved["completion"], "reused": True})
                return saved["completion"]
            path.rename(path.with_name(path.stem + f"-{time.time_ns()}.json"))
        artifact = {"stage": stage, "request": request, "created_at": datetime.now(timezone.utc).isoformat()}
        started = time.monotonic()
        try:
            result = await self.provider.complete(**request, cls=Class.BATCH)
            artifact.update(status="completed", completion=asdict(result))
        except AdmissionRejected as exc:
            # Inspect allowlisted limit labels only; never persist upstream prose,
            # credentials, echoed source text, or full exception representations.
            diagnostic_text = str(exc.__cause__ or "")
            labels = ("tokens per minute", "tokens per day", "tokens per hour",
                      "output tokens", "input tokens", "request too large", "OTPM", "ITPM", "TPD", "TPM")
            artifact.update(status="deferred", category=exc.category,
                            retry_after_s=exc.retry_after_s, quota=exc.rate_limit_details,
                            exact_hint=exc.exact_hint, rate_limits=exc.rate_limits,
                            limit_labels=[label for label in labels if re.search(re.escape(label), diagnostic_text, re.I)])
            raise
        except Exception as exc:
            artifact.update(status="failed", error_type=type(exc).__name__, category=getattr(exc, "category", None))
            # Groq may return generated text separately on JSON validation failures.
            # Keep only that field in the ignored local artifact, never error prose.
            cause = exc.__cause__
            response = getattr(cause, "response", None)
            try:
                body = response.json() if response is not None else getattr(cause, "body", None)
                if isinstance(body, dict):
                    generated = body.get("error", {}).get("failed_generation")
                    if isinstance(generated, str):
                        artifact["failed_generation"] = generated
            except Exception:
                pass
            raise
        finally:
            artifact["elapsed_s"] = round(time.monotonic() - started, 3)
            save(path, artifact)
            print(json.dumps({"stage": stage, "status": artifact["status"], "artifact": str(path),
                              "input_tokens": artifact.get("completion", {}).get("input_tokens"),
                              "output_tokens": artifact.get("completion", {}).get("output_tokens"),
                              "retry_after_s": artifact.get("retry_after_s")}), flush=True)
        self.attempts.append({"stage": stage, "artifact": str(path), "completion": artifact["completion"], "reused": False})
        return artifact["completion"]


class LocalBatch:
    """Use real stage request construction, without creating persistent batch jobs."""
    def __init__(self, calls):
        self.calls, self.stage = calls, "identity"

    async def batch_submit(self, requests):
        assert len(requests) == 1
        return requests[0]

    async def batch_poll(self, request):
        completion = await self.calls.call(self.stage, {k: v for k, v in request.items() if k != "id"})
        return [{"id": request["id"], "output": completion["text"],
                 "served_provider": completion["served_provider"], "served_model": completion["served_model"]}]

    def require_single_result(self, key, results):
        assert len(results) == 1 and results[0]["id"] == key
        return results[0]


async def run(args):
    saved = json.loads(args.extraction.read_text())
    case = saved["case"]
    if case["chapter"] != 1:
        raise ValueError("This first-chapter trial has no earlier identity candidates")
    cfg = Config.load()
    async with await psycopg.AsyncConnection.connect(cfg.database_url, autocommit=True,
            options="-c default_transaction_read_only=on") as db:
        config = await resolve_provider_config(db, saved["novel_id"], cfg.llm_provider)
        row = await (await db.execute("SELECT source_lang,target_lang FROM novel WHERE id=%s",
                                     (saved["novel_id"],))).fetchone()
    assert config is not None and row is not None
    args.output.mkdir(parents=True, exist_ok=True)
    provider = build_provider(config, cfg)
    calls = LocalCalls(provider, args.output)
    # This is a new experiment adapter, not validation under the old atomic-claim
    # schema: grouped notes may cite more than four passages. Preserve all of them.
    passages = {p["id"]: p for p in _source_passages(case["source"])}
    claims = []
    for i, note in enumerate(saved["memory"]["notes"], 1):
        assert isinstance(note["note"], str) and note["note"].strip()
        assert note["passages"] and all(p in passages for p in note["passages"])
        claims.append({"claim_id": f"c{i}", "claim_source": note["note"],
                       "evidence_ids": note["passages"], "context_ids": [],
                       "evidence": [passages[p] for p in note["passages"]], "context": []})
    discovery = {"accepted": claims, "rejected": [], "adapter": "grouped-notes-v1"}
    summary = {"extraction_artifact": str(args.extraction), "case": case,
               "discovery": discovery, "status": "started", "published": False,
               "limitations": ["Chapter 1 only; earlier identity candidates are empty.",
                               "Storage and reader API are not exercised; outputs remain local."]}
    try:
        request = normalization_request(case, discovery, args.model, args.normalization_tokens,
                                        reasoning_effort="low", variant="assertion")
        completion = await calls.call("normalize", request)
        normalized = validate_extraction(completion["text"], case,
                                        normalization_input(case, discovery, "assertion"), require_assertions=True)
        summary["normalized"] = normalized
        save(args.output / "summary.json", summary)
        records = normalized.get("accepted", {})
        print(json.dumps({"accepted": {k: len(v) for k, v in records.items()},
                          "rejected": len(normalized.get("rejected", []))}), flush=True)
        if not any(records.get(k) for k in ("facts", "relations", "events")):
            summary["status"] = "no_accepted_records"
            return
        batch = LocalBatch(calls)
        ctx = SimpleNamespace(cfg=replace(cfg, hosted_graph_output_tokens=4096),
                              novel=SimpleNamespace(ontology=case["ontology"], source_lang=row[0], target_lang=row[1]),
                              provider_id=config.provider, model_override=args.model, batch_manager=batch)
        state = SimpleNamespace(envelope=SimpleNamespace(raw_text=case["source"], chapter_index=case["chapter"],
                                source_meta=SimpleNamespace(raw_hash=saved["source_hash"])))
        names = [{"id": e["local_id"], "name": e["canonical_source"],
                  "mentions": e.get("source_aliases", []), "passages": e.get("evidence_ids", [])}
                 for e in records.get("entities", [])]
        stage = RecordsStage()
        summary["resolution"] = await stage._resolve(ctx, state, names, [])
        save(args.output / "summary.json", summary)
        batch.stage = "render"
        summary["renderings"] = await stage._render_fact_first(ctx, state, normalized)
        summary["status"] = "completed_local_stages"
    except AdmissionRejected as exc:
        summary.update(status="deferred", retry_after_s=exc.retry_after_s, quota=exc.rate_limit_details)
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, category=getattr(exc, "category", None))
    finally:
        summary["attempts"] = calls.attempts
        summary["usage"] = {k: sum(a["completion"].get(k, 0) for a in calls.attempts)
                            for k in ("input_tokens", "output_tokens")}
        summary["extraction_usage"] = {k: saved["completion"].get(k, 0) for k in ("input_tokens", "output_tokens")}
        save(args.output / "summary.json", summary)
        await provider.aclose()
        print(json.dumps({"status": summary["status"], "summary": str(args.output / "summary.json"),
                          "usage": summary["usage"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="qwen/qwen3.8-27b")
    parser.add_argument("--normalization-tokens", type=int, default=8192)
    asyncio.run(run(parser.parse_args()))
