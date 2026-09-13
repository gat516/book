"""Durable checkpoints for the fact-first extraction contract.

The worker may be interrupted between model calls.  These helpers keep the exact
model responses and validator outputs in generation-scoped rows; they never infer an
identity from spelling.  Local IDs remain local until the explicit who's-who pass.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from pipeline.fact_first import _source_passages


def run_id(generation_id: str, chapter: int) -> str:
    return str(uuid.uuid5(uuid.UUID(generation_id), f"fact-first:run:{chapter}"))


def _payload(value: Any) -> Jsonb:
    return Jsonb(value if isinstance(value, (dict, list)) else {"value": value})


async def ensure_run(ctx, state, *, baseline_commit: str, prompt_hashes: dict[str, str] | None = None,
                     prompt_variants: dict[str, str] | None = None, budgets: dict[str, Any] | None = None) -> str:
    gid = state.record_generation_id
    if not gid:
        raise ValueError("fact-first persistence requires a generation pin")
    rid = run_id(gid, state.envelope.chapter_index)
    existing = await (await ctx.db.execute(
        "SELECT status,source_hash FROM fact_first_run WHERE id=%s", (rid,))).fetchone()
    if existing:
        if existing[1] != state.envelope.source_meta.raw_hash:
            raise ValueError("fact-first run source hash changed inside immutable generation")
        if existing[0] == "discarded":
            raise ValueError("fact-first run was discarded; explicit retry must clear the discard")
    await ctx.db.execute(
        """INSERT INTO fact_first_run
          (id,novel_id,generation_id,chapter_index,source_hash,request_identity,baseline_commit,
           prompt_hashes,prompt_variants,provider,requested_model,budgets)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (novel_id,generation_id,chapter_index) DO UPDATE SET
            status='processing',
            prompt_hashes=COALESCE(NULLIF(EXCLUDED.prompt_hashes,'{}'::jsonb),fact_first_run.prompt_hashes),
            prompt_variants=COALESCE(NULLIF(EXCLUDED.prompt_variants,'{}'::jsonb),fact_first_run.prompt_variants),
            budgets=COALESCE(NULLIF(EXCLUDED.budgets,'{}'::jsonb),fact_first_run.budgets)
          WHERE fact_first_run.status NOT IN ('published','discarded')""",
        (rid, ctx.novel.id, gid, state.envelope.chapter_index,
         state.envelope.source_meta.raw_hash,
         hashlib.sha256(f"fact-first:{gid}:{state.envelope.chapter_index}".encode()).hexdigest(),
         baseline_commit, _payload(prompt_hashes or {}), _payload(prompt_variants or {}),
         ctx.provider_id or ctx.cfg.llm_provider,
         state.record_generation_config.get("requested_model", "") if state.record_generation_config else "",
         _payload(budgets or {})),
    )
    return rid


async def save_discovery(ctx, state, run: str, discovery: dict[str, Any]) -> None:
    """Checkpoint discovery atomically, including passages and candidates."""
    async with ctx.db.transaction():
        await _save_discovery(ctx, state, run, discovery)


async def _save_discovery(ctx, state, run: str, discovery: dict[str, Any]) -> None:
    passages = _source_passages(state.envelope.raw_text)
    await ctx.db.execute("DELETE FROM fact_first_passage WHERE run_id=%s", (run,))
    async with ctx.db.cursor() as cur:
        await cur.executemany(
            "INSERT INTO fact_first_passage(run_id,passage_id,text,char_start,char_end,ordinal) VALUES (%s,%s,%s,%s,%s,%s)",
            [(run, p["id"], p["text"], p["char_start"], p["char_end"], i) for i, p in enumerate(passages)])
    await ctx.db.execute("DELETE FROM fact_first_candidate WHERE run_id=%s", (run,))
    async with ctx.db.cursor() as cur:
        await cur.executemany(
            """INSERT INTO fact_first_candidate
               (run_id,candidate_id,source_text,passage_ids,char_start,char_end,ordinal,payload,status,rejection_reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(run, row.get("claim_id", f"__rejected_{i}"), row.get("claim_source", row.get("row", {}).get("claim_source", "")) or "",
              Jsonb(row.get("evidence_ids", [])),
              next((p["char_start"] for p in passages if p["id"] in row.get("evidence_ids", [])), None),
              next((p["char_end"] for p in reversed(passages) if p["id"] in row.get("evidence_ids", [])), None),
              i, Jsonb(row), "accepted", None)
             for i, row in enumerate(discovery.get("accepted", []))] +
            [(run, f"__rejected_{i}_{(row.get('row', {}) or {}).get('claim_id', 'unknown')}",
              (row.get("row", {}) or {}).get("claim_source", row.get("raw", "")) or "",
              Jsonb((row.get("row", {}) or {}).get("evidence_ids", [])), None, None,
              len(discovery.get("accepted", [])) + i, Jsonb(row), "rejected", row.get("reason", "rejected"))
             for i, row in enumerate(discovery.get("rejected", []))])
    await ctx.db.execute(
        "UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
        (Jsonb({"discovery": discovery}), run))


async def save_selection(ctx, run: str, selection: dict[str, Any]) -> None:
    """Checkpoint the compact selection atomically."""
    async with ctx.db.transaction():
        await _save_selection(ctx, run, selection)


async def _save_selection(ctx, run: str, selection: dict[str, Any]) -> None:
    await ctx.db.execute("DELETE FROM fact_first_selection WHERE run_id=%s", (run,))
    async with ctx.db.cursor() as cur:
        await cur.executemany(
            "INSERT INTO fact_first_selection(run_id,candidate_id,decision,kind,consolidate_into,reason,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(run, row["claim_id"], row["action"], row.get("kind") or None, row.get("into") or None, row.get("reason", ""), Jsonb(row))
             for row in selection.get("decisions", [])])
    await ctx.db.execute("UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
                         (Jsonb({"selection": selection}), run))


async def save_normalization(ctx, run: str, normalized: dict[str, Any]) -> None:
    attempts = normalized.get("attempts") or []
    hashes: dict[str, str] = {}
    served: dict[str, dict[str, str]] = {}
    budgets: dict[str, Any] = {}
    for attempt in attempts:
        stage = attempt.get("stage")
        request = attempt.get("request") or {}
        if stage and isinstance(request, dict):
            material = json.dumps({"system": request.get("system", ""),
                                   "prompt": request.get("prompt", "")},
                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            hashes[str(stage)] = hashlib.sha256(material.encode()).hexdigest()
            if request.get("max_output_tokens") is not None:
                budgets[str(stage)] = request["max_output_tokens"]
        if stage and attempt.get("served_provider"):
            served[str(stage)] = {"provider": str(attempt["served_provider"]),
                                  "model": str(attempt.get("served_model", ""))}
    await ctx.db.execute(
        """UPDATE fact_first_run SET diagnostics=diagnostics || %s,
             prompt_hashes=prompt_hashes || %s, budgets=budgets || %s,
             provider=COALESCE(%s,provider), served_model=COALESCE(%s,served_model)
           WHERE id=%s""",
        (Jsonb({"normalization": normalized, "served_by_stage": served}), Jsonb(hashes), Jsonb(budgets),
         (next(iter(served.values()), {}).get("provider") if served else None),
         normalized.get("served_model") or (next(iter(served.values()), {}).get("model") if served else None), run))


async def load_checkpoints(ctx, run: str) -> dict[str, Any]:
    """Read the durable stage prefix used by a resumed chapter job."""
    row = await (await ctx.db.execute(
        "SELECT diagnostics FROM fact_first_run WHERE id=%s", (run,))).fetchone()
    diagnostics = row[0] if row and isinstance(row[0], dict) else {}
    checkpoints = {key: diagnostics[key] for key in ("discovery", "selection", "normalization")
                   if isinstance(diagnostics.get(key), dict)}
    checkpoints["_attempts"] = [diagnostics[key] for key in
                                 ("discovery_attempt", "selection_attempt", "normalization_attempt")
                                 if isinstance(diagnostics.get(key), dict)]
    return checkpoints
