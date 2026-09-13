"""Records enrichment: discovery, authoritative who's-who, and offline rendering."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from pipeline.context import PipelineState, StageContext
from pipeline.jobs import model_for_stage
from pipeline.llm.provider import AdmissionRejected, BatchRequest, Class
from psycopg.types.json import Jsonb
from pipeline.records import unresolved, validate_resolution
from pipeline.records_generation import mark_record_processing, prepare_generation
from pipeline.records_prompts import RENDER_SYSTEM, RESOLVE_SYSTEM
from pipeline.fact_first import (
    discovery_request, normalization_request, validate_discovery, validate_extraction,
    normalization_input, CLAIM_CEILINGS, _xml_body, _source_passages,
)
from pipeline.benchmark_selection import selection_request, validate_selection, selected_discovery
from pipeline.fact_first_persistence import ensure_run, load_checkpoints, save_discovery, save_selection, save_normalization

log = logging.getLogger(__name__)


def _decode_rendering(text: str) -> dict:
    """Decode the rendering object, tolerating only an unambiguous JSON fence."""
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.I | re.S)
    if fenced:
        candidate = fenced.group(1)
    decoded = json.loads(candidate)
    if not isinstance(decoded, dict):
        raise ValueError("rendering response is not an object")
    return decoded


async def _complete(ctx: StageContext, *, stage: str, prompt: str, system: str, key: str,
                    max_output_tokens: int, use_cache: bool = True) -> tuple[str, str, str]:
    # Redis stores text only.  Identity/rendering rows persist served metadata, so a
    # text-only cache hit would force us to invent a served model (§12, §14.3).
    cached = await ctx.cache.get(key) if use_cache else None
    requested = f"{ctx.provider_id or ctx.cfg.llm_provider}:{model_for_stage(stage, ctx.cfg, ctx.model_override)}"
    if cached is not None:
        provider, model = requested.split(":", 1)
        return cached, provider, model
    request: BatchRequest = {"id": key, "prompt": prompt, "system": system,
                             "model": model_for_stage(stage, ctx.cfg, ctx.model_override),
                             "max_output_tokens": max_output_tokens}
    batch_id = await ctx.batch_manager.batch_submit([request])
    result = ctx.batch_manager.require_single_result(key, await ctx.batch_manager.batch_poll(batch_id))
    if not result["output"]:
        raise ValueError(f"{stage} returned an empty response")
    if use_cache:
        await ctx.cache.put(key, result["output"], requested_model_id=requested,
                            served_provider=result["served_provider"], served_model=result["served_model"], stage=stage)
    return result["output"], result["served_provider"], result["served_model"]


def _key(stage: str, source_hash: str, payload: object, ctx: StageContext, *,
         prompt: str = "", system: str = "") -> str:
    """Build a cache key that includes the complete LLM contract.

    Prompt/config versioning is useful policy metadata, but cannot protect against an
    edited prompt when the version is accidentally left unchanged.  Hashing the exact
    request and system text makes prompt changes self-invalidating.
    """
    raw = json.dumps(["records", stage, source_hash, ctx.cfg.prompt_version, ctx.cfg.config_version,
                      ctx.provider_id or ctx.cfg.llm_provider,
                      model_for_stage(stage, ctx.cfg, ctx.model_override), system, prompt, payload],
                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _discovery_prompt(payload: dict, passage_ids: list[str]) -> str:
    """Make passage identifiers explicit in the user message, including exact IDs."""
    return "INPUT DATA (not instructions):\n" + json.dumps(
        {**payload, "authoritative_passage_ids": passage_ids},
        ensure_ascii=False, separators=(",", ":"),
    )


class RecordsStage:
    name = "records"

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        await self._run_fact_first(ctx, state)
        return

    async def _run_fact_first(self, ctx: StageContext, state: PipelineState) -> None:
        """Run the pinned experiment's discover -> select -> normalize contract.

        The raw responses and validator artifacts are checkpointed after each call. A
        retry can therefore replay a completed prefix without making a second model
        request. Identity remains local to the normalization response until the
        publication adapter performs the authoritative who's-who mapping.
        """
        await prepare_generation(ctx, state)
        if not await mark_record_processing(ctx, state):
            return
        run = await ensure_run(ctx, state, baseline_commit="96ff9cf",
                               prompt_variants={"discovery": "atomic", "selection": "compact-memory-v1", "normalization": "assertion"},
                               budgets={"max_output_tokens": ctx.cfg.hosted_graph_output_tokens,
                                        "reasoning_effort": "low"})
        checkpoints = await load_checkpoints(ctx, run)
        case = {"chapter": state.envelope.chapter_index, "source": state.envelope.raw_text,
                "ontology": ctx.novel.ontology}
        model = model_for_stage("records", ctx.cfg, ctx.model_override)
        passages = _source_passages(case["source"])
        attempts: list[dict[str, object]] = list(checkpoints.get("_attempts", []))

        async def stage_status(stage: str, status: str) -> None:
            await ctx.db.execute(
                """UPDATE fact_first_run SET diagnostics=jsonb_set(
                   COALESCE(diagnostics,'{}'::jsonb), '{stages}',
                   COALESCE(diagnostics->'stages','{}'::jsonb) || %s::jsonb, true)
                   WHERE id=%s""",
                (Jsonb({stage: status}), run),
            )

        async def call(request: dict[str, object]) -> tuple[str, str, str]:
            """One scheduler-mediated call; admission retries belong to the worker."""
            kwargs = {"system": request["system"], "cls": Class.BATCH,
                      "model": request["model"], "json_schema": request.get("json_schema"),
                      "json_mode": bool(request.get("json_mode", False)),
                      "max_output_tokens": request["max_output_tokens"]}
            if (ctx.provider_id or ctx.cfg.llm_provider) in {"groq", "openrouter"}:
                kwargs["reasoning_effort"] = request.get("reasoning_effort", "low")
            completion = await ctx.provider.complete(request["prompt"], **kwargs)
            attempts.append({"stage": request["stage"], "request": request,
                             "response": completion.text, "status": "completed",
                             "served_provider": completion.served_provider,
                             "served_model": completion.served_model,
                             "usage": {"input_tokens": completion.input_tokens,
                                       "output_tokens": completion.output_tokens,
                                       "cache_read_tokens": completion.cache_read_tokens,
                                       "cache_write_tokens": completion.cache_write_tokens}})
            return completion.text, completion.served_provider, completion.served_model

        discovery = checkpoints.get("discovery")
        if isinstance(discovery, dict) and isinstance(discovery.get("response"), str):
            discovery = validate_discovery(discovery["response"], case,
                                           claim_ceiling=CLAIM_CEILINGS["atomic"],
                                           variant="atomic")
            discovery["response"] = checkpoints["discovery"]["response"]
            await stage_status("discovery", "completed")
        else:
            await stage_status("discovery", "processing")
            request = discovery_request(case, model, ctx.cfg.hosted_graph_output_tokens,
                                        variant="atomic")
            discovery_raw, discovery_provider, discovery_model = await call(request)
            discovery = validate_discovery(discovery_raw, case,
                                           claim_ceiling=CLAIM_CEILINGS["atomic"],
                                           variant="atomic")
            discovery["response"] = discovery_raw
            await ctx.db.execute("UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
                                 (Jsonb({"discovery_attempt": attempts[-1]}), run))
            await save_discovery(ctx, state, run, discovery)
            await stage_status("discovery", "completed")

        selection = checkpoints.get("selection")
        if not discovery.get("accepted"):
            selection = validate_selection("<selection/>", discovery,
                                          state.envelope.source_meta.raw_hash)
            selection["response"] = "<selection/>"
            await save_selection(ctx, run, selection)
            await stage_status("selection", "completed")
        elif isinstance(selection, dict) and isinstance(selection.get("response"), str):
            selection = validate_selection(_xml_body(selection["response"]), discovery,
                                          state.envelope.source_meta.raw_hash)
            await stage_status("selection", "completed")
        else:
            await stage_status("selection", "processing")
            select_req = selection_request(case, discovery, passages, model,
                                           ctx.cfg.hosted_graph_output_tokens, "low")
            selection_raw, selection_provider, selection_model = await call(select_req)
            selection = validate_selection(_xml_body(selection_raw), discovery,
                                          state.envelope.source_meta.raw_hash)
            # selected_discovery revalidates the exact XML body; fences and prose are
            # validator input only and must never become the persisted replay payload.
            selection["response"] = _xml_body(selection_raw)
            await save_selection(ctx, run, selection)
            await ctx.db.execute("UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
                                 (Jsonb({"selection_attempt": attempts[-1]}), run))
            await stage_status("selection", "completed")
        selected = selected_discovery(discovery, selection, state.envelope.source_meta.raw_hash)

        normalized = checkpoints.get("normalization")
        saved_renderings = normalized.get("renderings") if isinstance(normalized, dict) else None
        saved_provider = normalized.get("served_provider") if isinstance(normalized, dict) else None
        saved_model = normalized.get("served_model") if isinstance(normalized, dict) else None
        if not selected.get("accepted"):
            await stage_status("normalization", "completed")
            norm_raw = "<knowledge><decisions/><entities/><facts/><relations/><events/></knowledge>"
            norm_provider, norm_model = ctx.provider_id, model
            normalized = validate_extraction(norm_raw, case,
                                             normalization_input(case, selected, "assertion"),
                                             require_assertions=True)
            normalized["normalization_skipped"] = "no_selected_claims"
        elif isinstance(normalized, dict) and isinstance(normalized.get("response"), str):
            await stage_status("normalization", "completed")
            norm_raw = normalized["response"]
            normalized = validate_extraction(
                norm_raw, case, normalization_input(case, selected, "assertion"),
                require_assertions=True)
            if isinstance(saved_renderings, dict):
                normalized["renderings"] = saved_renderings
            norm_provider = saved_provider or ctx.provider_id
            norm_model = saved_model or model
        elif not isinstance(normalized, dict) or not isinstance(normalized.get("accepted"), dict):
            await stage_status("normalization", "processing")
            norm_req = normalization_request(case, selected, model,
                                             ctx.cfg.hosted_graph_output_tokens, variant="assertion")
            norm_raw, norm_provider, norm_model = await call(norm_req)
            normalized = validate_extraction(norm_raw, case,
                                             normalization_input(case, selected, "assertion"),
                                             require_assertions=True)
            await stage_status("normalization", "completed")
        else:
            norm_raw = normalized.get("response", "")
            norm_provider = normalized.get("served_provider", ctx.provider_id)
            norm_model = normalized.get("served_model", model)
        if attempts and attempts[-1].get("stage") == "normalize":
            await ctx.db.execute("UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
                                 (Jsonb({"normalization_attempt": attempts[-1]}), run))
        normalized.update({"response": norm_raw, "attempts": attempts,
                           "source_hash": state.envelope.source_meta.raw_hash,
                           "baseline_commit": "96ff9cf", "selection": selection,
                           "discovery": discovery,
                           "served_provider": norm_provider, "served_model": norm_model})
        # Checkpoint the validated prefix before the live identity/rendering passes;
        # either later pass may be admitted/retried independently.
        await save_normalization(ctx, run, normalized)
        # The publisher consumes the validated native rows directly. Keep the exact
        # local IDs and evidence dictionaries for the persistence layer and reader.
        normalized["fact_first_run_id"] = run
        state.records = {"fact_first": normalized, "attempts": attempts,
                         "served_provider": norm_provider, "served_model": norm_model,
                         "source_hash": state.envelope.source_meta.raw_hash,
                         "request_identity": hashlib.sha256(json.dumps(
                             [{"stage": a.get("stage"), "request": a.get("request")}
                              for a in attempts], sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()}
        # `name_map` is only the English rendering map. Identity authorization comes
        # from the explicit chronological who's-who pass below.
        names = []
        for entity in normalized.get("accepted", {}).get("entities", []):
            names.append({"id": entity["local_id"], "name": entity["canonical_source"],
                          "mentions": entity.get("source_aliases", []),
                          "passages": entity.get("evidence_ids", [])})
        await stage_status("identity_resolution", "processing")
        try:
            candidates = await self._candidates(ctx, state.envelope.chapter_index, names,
                                                state.record_generation_id)
            resolution = (unresolved(names, "no entity proposals") if not names else
                          await self._resolve(ctx, state, names, candidates))
        except Exception:
            await stage_status("identity_resolution", "failed")
            raise
        normalized["resolution"] = resolution
        state.resolutions = resolution.get("name_map", {})
        await stage_status("identity_resolution", "completed")
        await stage_status("rendering", "processing")
        try:
            if not isinstance(normalized.get("renderings"), dict):
                normalized["renderings"] = await self._render_fact_first(ctx, state, normalized)
        except Exception:
            await stage_status("rendering", "failed")
            raise
        render_failed = any(isinstance(value, dict) and value.get("status") == "failed"
                            for value in (normalized.get("renderings") or {}).values())
        await stage_status("rendering", "failed" if render_failed else "completed")
        await ctx.db.execute("UPDATE fact_first_run SET diagnostics=diagnostics || %s WHERE id=%s",
                             (Jsonb({"attempts": attempts,
                                     "prompt_hashes": {str(a.get("stage")): hashlib.sha256(
                                         json.dumps({"system": a.get("request", {}).get("system", ""),
                                                     "prompt": a.get("request", {}).get("prompt", "")},
                                                    sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()
                                         for a in attempts}}), run))
        exact_prompt_hashes = {
            str(a.get("stage")): hashlib.sha256(json.dumps(
                {"system": a.get("request", {}).get("system", ""),
                 "prompt": a.get("request", {}).get("prompt", "")},
                sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()).hexdigest()
            for a in attempts if isinstance(a.get("request"), dict)
        }
        await ctx.db.execute(
            """UPDATE fact_first_run SET prompt_hashes=%s, provider=%s,
               requested_model=%s, served_model=%s, budgets=%s WHERE id=%s""",
            (Jsonb(exact_prompt_hashes), norm_provider, model, norm_model,
             Jsonb({"max_output_tokens": ctx.cfg.hosted_graph_output_tokens,
                    "reasoning_effort": "low"}), run))
        await save_normalization(ctx, run, normalized)
        log.info("fact-first prepared chapter=%s candidates=%s selected=%s rejected=%s",
                 state.envelope.chapter_index, len(discovery.get("accepted", [])),
                 len(selected.get("accepted", [])), len(normalized.get("rejected", [])))

        return

    async def _candidates(self, ctx: StageContext, chapter: int, names: list[dict],
                          generation_id: str | None) -> list[dict]:
        # Candidate discovery is fenced to the generation selected before who's-who.
        # Never reread novel.active_record_generation here: a rebuild can switch it
        # between preparation and this query, and mixing candidates would corrupt the
        # chapter's authoritative resolution.
        if not generation_id:
            return []
        rows = await (await ctx.db.execute("""SELECT DISTINCT e.id,e.canonical,e.kind
             FROM entity e JOIN alias a ON a.entity_id=e.id
            WHERE e.novel_id=%s AND e.record_generation_id=%s AND e.first_seen_chapter < %s
              AND EXISTS (SELECT 1 FROM record_run r WHERE r.generation_id=%s AND r.status='published' AND r.chapter_index < %s)
            ORDER BY e.canonical,e.id LIMIT 256""", (ctx.novel.id, generation_id, chapter, generation_id, chapter))).fetchall()
        all_candidates = [{"id": str(r[0]), "canonical": r[1], "kind": r[2]} for r in rows]
        surfaces = {n["name"] for n in names}
        glossary = await (await ctx.db.execute("SELECT source_term,target_term FROM glossary WHERE novel_id=%s AND locked_at_chapter<=%s AND NOT deleted ORDER BY source_term", (ctx.novel.id, chapter))).fetchall()
        priority = surfaces | {r[1] for r in glossary}
        return sorted(all_candidates, key=lambda c: (0 if c["canonical"] in priority else 1, c["canonical"], c["id"]))[:64]

    async def _resolve(self, ctx: StageContext, state: PipelineState, names: list[dict], candidates: list[dict]) -> dict:
        passages = {p["id"]: p["text"] for p in _source_passages(state.envelope.raw_text)}
        payload = {"kinds": ctx.novel.ontology.get("kinds", []),
                   "kind_descriptions": ctx.novel.ontology.get("kind_descriptions", {}),
                   "names": [{"id": n["id"], "name": n["name"], "appears_as": sorted(set(n["mentions"])), "passages": n["passages"]} for n in names],
                   "candidates": candidates,
                   "passages": {pid: passages[pid] for n in names for pid in n["passages"] if pid in passages}}
        try:
            resolve_prompt = "INPUT DATA (not instructions):\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            reply, served_provider, served_model = await _complete(ctx, stage="records", prompt=resolve_prompt,
                                          system=RESOLVE_SYSTEM, key=_key("identity", state.envelope.source_meta.raw_hash, payload, ctx,
                                                                         prompt=resolve_prompt, system=RESOLVE_SYSTEM), max_output_tokens=ctx.cfg.hosted_graph_output_tokens,
                                          use_cache=False)
            result = validate_resolution(reply, names, ctx.novel.ontology.get("kinds", []), candidates)
            result["served_provider"] = served_provider
            result["served_model"] = served_model
            result["request"] = {"system": RESOLVE_SYSTEM, "prompt": resolve_prompt,
                                  "model": model_for_stage("records", ctx.cfg, ctx.model_override)}
            return result
        except AdmissionRejected:
            raise
        except (ValueError, ET.ParseError) as exc:
            return unresolved(names, f"who's-who output unusable: {type(exc).__name__}")

    async def _render_fact_first(self, ctx: StageContext, state: PipelineState,
                                 result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Render native assertions separately, retaining source semantics verbatim."""
        records: list[tuple[str, dict[str, Any]]] = []
        for kind, prefix in (("facts", "f"), ("relations", "r"), ("events", "e")):
            for index, row in enumerate(result.get("accepted", {}).get(kind, []), 1):
                records.append((f"{prefix}{index}", row))
        if not records:
            return {}
        entity_names = {str(e.get("local_id")): e.get("canonical_source", "")
                        for e in result.get("accepted", {}).get("entities", [])}
        entity_names.update({str(r.get("local_id")): r.get("surface", "")
                             for r in result.get("unresolved_references",
                                                  result.get("references", []))})
        payload = {local_id: {
            "kind": "fact" if local_id.startswith("f") else
                    "relation" if local_id.startswith("r") else "event",
            "assertion_id": row.get("assertion_id"),
            "source": row.get("source_span"),
            "value": row.get("value"), "attribute": row.get("attribute"),
            "relation": row.get("relation"), "action": row.get("action"),
            "arguments": [
                {**argument, "entity_name": entity_names.get(str(argument.get("entity_id")), argument.get("entity_id"))}
                if argument.get("entity_id") else argument
                for argument in row.get("arguments", [])
            ],
            "subject_name": entity_names.get(str(row.get("subject_id", "")), row.get("subject_id")),
            "src_name": entity_names.get(str(row.get("src_id", "")), row.get("src_id")),
            "dst_name": entity_names.get(str(row.get("dst_id", "")), row.get("dst_id")),
            "polarity": row.get("polarity"), "attribution": row.get("attribution"),
            "condition": row.get("condition"), "temporal": row.get("temporal"),
        } for local_id, row in records}
        if ctx.novel.source_lang == ctx.novel.target_lang:
            return {local_id: {"output_kind": "native_assertion", "assertion_id": row.get("assertion_id"),
                               "target_value": row.get("value") or row.get("source_span"),
                               "provider": "source", "served_model": "", "status": "ready"}
                    for local_id, row in records}
        try:
            prompt = json.dumps({"target_language": ctx.novel.target_lang, "assertions": payload},
                                ensure_ascii=False, separators=(",", ":"))
            reply, provider, served_model = await _complete(
                ctx, stage="records", prompt=prompt, system=RENDER_SYSTEM,
                key=_key("render", state.envelope.source_meta.raw_hash, payload, ctx,
                          prompt=prompt, system=RENDER_SYSTEM),
                max_output_tokens=ctx.cfg.hosted_graph_output_tokens, use_cache=False)
            decoded = _decode_rendering(reply)
            return {local_id: {"output_kind": "native_assertion", "assertion_id": row.get("assertion_id"),
                               "target_value": decoded[local_id], "provider": provider,
                               "served_model": served_model, "status": "ready"}
                    if local_id in decoded and isinstance(decoded[local_id], (str, int, float))
                    else {"output_kind": "native_assertion", "assertion_id": row.get("assertion_id"),
                          "target_value": None, "provider": provider, "served_model": served_model,
                          "status": "failed", "error_detail": "field omitted"}
                    for local_id, row in records}
        except AdmissionRejected:
            raise
        except Exception as exc:
            return {local_id: {"output_kind": "native_assertion", "assertion_id": row.get("assertion_id"),
                               "target_value": None, "provider": "", "served_model": "",
                               "status": "failed", "error_detail": type(exc).__name__}
                    for local_id, row in records}
