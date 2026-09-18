"""Durable, bounded local hosted jobs for evidence experiments (§0.7, §5.4, §6.1).

Each attempt is recorded before submission. Successful responses are saved with
served-model provenance before validation, so parser fixes never require regeneration.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time

from evidence_memory import digest, wire
from pipeline.llm.provider import AdmissionRejected, Class


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    pending.replace(path)


def freeze_json(path, value):
    """An artifact is immutable once written; changed inputs use another identity."""
    path = Path(path)
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError("immutable artifact differs; use a new experiment directory")
    else:
        write_json(path, value)


class JobStopped(RuntimeError):
    pass


class EvidenceJobs:
    def __init__(self, root, provider_identity, *, max_attempts=1, interval=60,
                 retry_failed=False, replay_only=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider_identity = provider_identity
        self.max_attempts, self.interval = max_attempts, interval
        self.retry_failed, self.replay_only = retry_failed, replay_only
        self.used, self.reused, self.new_attempts = {}, [], []
        self._lock = None

    def __enter__(self):
        self._lock = (self.root / ".lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise JobStopped("another evidence job is using this directory") from None
        try:
            freeze_json(self.root / "provider.json", self.provider_identity)
        except Exception:
            self._lock.close()
            raise
        return self

    def __exit__(self, *args):
        if self._lock:
            self._lock.close()

    def attempts(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / "attempts").glob("*.json"))]

    def key(self, request, context):
        # Both provider/endpoint and exact request scope are necessary (§6.1).
        return digest([self.provider_identity, context, request])

    def completed(self):
        return [a for a in self.attempts() if a["status"] == "completed"]

    async def call(self, stage, request, context, provider_factory):
        key = self.key(request, context)
        attempts = self.attempts()
        matching = [a for a in attempts if a["key"] == key]
        successful = next((a for a in matching if a["status"] == "completed"), None)
        if successful:
            result = json.loads((self.root / successful["response"]).read_text())
            self.used[key] = successful
            self.reused.append(key)
            return result["completion"], key
        if self.replay_only:
            raise JobStopped("no exact completed response available for offline replay")
        if matching and not self.retry_failed:
            raise JobStopped("previous attempt failed or was interrupted; explicit --retry-failed required")
        if len(attempts) >= self.max_attempts:
            raise JobStopped("persistent attempt budget exhausted; cached work remains available")
        deadline = max((a.get("retry_at", 0) for a in attempts), default=0)
        if time.time() < deadline:
            raise JobStopped(f"provider cooldown: retry in {int(deadline - time.time()) + 1} seconds")
        last = max((a.get("finished_at", a["started_at"]) for a in attempts), default=0)
        delay = self.interval - (time.time() - last)
        if delay > 0:
            print(wire({"stage": stage, "pacing_seconds": round(delay, 1)}), flush=True)
        while delay > 0:
            await asyncio.sleep(min(delay, 30))
            delay = self.interval - (time.time() - last)
        number = len(attempts) + 1
        path = self.root / "attempts" / f"{number:05d}.json"
        attempt = {"number": number, "key": key, "stage": stage, "context": context,
                   "request": request, "status": "started", "started_at": time.time(),
                   "created_at": datetime.now(timezone.utc).isoformat(), "usage": None}
        # A killed process leaves a started attempt, counted on the next run.
        write_json(path, attempt)
        self.new_attempts.append(number)
        provider = None
        try:
            provider = provider_factory()
            completion = asdict(await provider.complete(**request, cls=Class.BATCH))
            if not completion.get("served_provider") or not completion.get("served_model"):
                raise ValueError("completion lacks served provider/model provenance")
            result = {"key": key, "completion": completion}
            response_key = digest([key, completion["served_provider"], completion["served_model"]])
            response_path = Path("responses") / f"{response_key}.json"
            freeze_json(self.root / response_path, result)
            attempt.update(status="completed", response=str(response_path),
                           usage={k: completion.get(k, 0) for k in
                                  ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")})
            self.used[key] = attempt
            return completion, key
        except AdmissionRejected as exc:
            attempt.update(status="deferred", error_type=type(exc).__name__, category=exc.category,
                           quota=exc.rate_limit_details, retry_after_s=exc.retry_after_s)
            if exc.retry_after_s is not None:
                attempt["retry_at"] = time.time() + max(0, exc.retry_after_s)
            raise
        except Exception as exc:
            # No upstream exception prose, credentials, or tracebacks in artifacts.
            attempt.update(status="failed", error_type=type(exc).__name__, category=getattr(exc, "category", None))
            raise
        finally:
            attempt["finished_at"] = time.time()
            write_json(path, attempt)
            print(wire({"stage": stage, "status": attempt["status"], "attempt": number,
                        "usage": attempt["usage"]}), flush=True)
            if provider is not None:
                await provider.aclose()

    def report(self):
        attempts = self.attempts()
        def usage(rows):
            rows = list(rows)
            return {k: sum((a.get("usage") or {}).get(k, 0) for a in rows)
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
        return {"attempts_total": len(attempts), "new_attempts": len(self.new_attempts),
                "unknown_usage_attempts": sum(a.get("usage") is None for a in attempts),
                "cumulative_successful_usage": usage(attempts),
                "new_successful_usage": usage(a for a in attempts if a["number"] in self.new_attempts),
                "reused_requests": len(set(self.reused)), "usage_scope": "Unknown attempt usage is not zero."}
