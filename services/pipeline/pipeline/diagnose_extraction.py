"""Offline extraction diagnostic for structured request sizing and transport.

This module intentionally imports only the provider protocol and pure passage packer.
It never opens a database, creates a revision, calls graph publication, or reads a
saved chapter. The default provider is deterministic, making the command useful on a
machine with no model server while preserving the same request accounting used by a
real provider supplied by a caller.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.batch import classify_batch_error
from pipeline.passages import conservative_token_estimate, pack_passages
from pipeline.inference_runtime import effective_schema_transport
from pipeline.llm.provider import AdmissionRejected, Class, Completion, LLMProvider


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array", "maxItems": 4,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["statement", "passage_ids"],
                      "properties": {"statement": {"type": "string"},
                                     "passage_ids": {"type": "array", "items": {"type": "string"}}}},
        },
    },
}


CASES = (
    ("negation", "林峰没有杀死赵云。", "保留否定，不把没有杀死改成杀死"),
    ("relationship_direction", "甲信任乙，但乙不信任甲。", "保留关系方向"),
    ("uncertain_identity", "他可能是陈家的人，但没人能确认。", "保留不确定性"),
    ("passage_boundary", "林峰走到门口。\n赵云随后打开了门。", "允许跨相邻段落引用"),
)
STATEMENTS = {case_id: source for case_id, source, _intent in CASES}


@dataclass(frozen=True)
class SyntheticProvider:
    """A local provider that returns one source-faithful claim for each case."""

    served_provider: str = "synthetic"
    served_model: str = "diagnostic"

    def schema_transport(self, model: str | None = None) -> str:
        """The deterministic adapter accepts the native schema protocol field."""
        del model
        return "native"

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                       cls: Class = Class.BATCH, pin_model: bool = False,
                       model: str | None = None, json_schema: dict | None = None,
                       max_output_tokens: int | None = None) -> Completion:
        # The case id is passed in the prompt's input data and is synthetic-only. No
        # chapter text is loaded or forwarded outside this process.
        data = json.loads(prompt.split("INPUT DATA:", 1)[-1])
        case_id = data["case_id"]
        source = data["source"]
        passages = data["passages"]
        statement = STATEMENTS[case_id]
        cited = [row["id"] for row in passages] if case_id == "passage_boundary" else [passages[0]["id"]]
        body = {"claims": [{"statement": statement, "passage_ids": cited}]}
        encoded = json.dumps(body, ensure_ascii=False)
        return Completion(text=encoded, served_provider=self.served_provider,
                          served_model=model or self.served_model,
                          input_tokens=len(prompt), output_tokens=len(encoded))


def _validate(body: Any, schema: dict, passages: list[dict], case_id: str) -> tuple[bool, str | None]:
    if not isinstance(body, dict) or set(body) != {"claims"} or not isinstance(body["claims"], list):
        return False, "response must contain only claims[]"
    if len(body["claims"]) > schema["properties"]["claims"]["maxItems"]:
        return False, "claims exceeds maxItems"
    for claim in body["claims"]:
        if not isinstance(claim, dict) or set(claim) != {"statement", "passage_ids"}:
            return False, "claim shape is invalid"
        if not isinstance(claim["statement"], str) or not isinstance(claim["passage_ids"], list):
            return False, "claim types are invalid"
        offered = {row["id"] for row in passages}
        if not claim["passage_ids"] or any(pid not in offered for pid in claim["passage_ids"]):
            return False, "claim cites an unavailable passage"
        if case_id == "passage_boundary" and len(claim["passage_ids"]) < 2:
            return False, "boundary claim must cite both adjacent passages"
    return True, None


def _error_metadata(exc: BaseException) -> dict:
    result = {"status": "failed", "error_class": classify_batch_error(exc)}
    if isinstance(exc, AdmissionRejected):
        result["rate_limit"] = {"category": exc.category,
                                 "retry_after_s": max(0.0, float(exc.retry_after_s)),
                                 "exact_hint": bool(exc.exact_hint)}
    return result


def _safe_rate_limits(completion: Completion) -> dict | None:
    metadata = getattr(completion, "rate_limits", None)
    if not isinstance(metadata, dict):
        return None
    # These are the only names the hosted adapter copies from HTTP responses. Keep
    # provider-specific quota values out of diagnostic output.
    allowed = {
        "retry-after", "ratelimit-reset", "x-ratelimit-reset",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
        "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
        "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
    }
    return {key: metadata[key] for key in allowed if key in metadata}


async def run_diagnostic(provider: LLMProvider | None = None, *, context_tokens: int = 2048,
                         output_tokens: int = 256, model: str | None = None) -> dict:
    """Run both duplicated-prompt and native-schema-only synthetic variants."""
    provider = provider or SyntheticProvider()
    model = model or getattr(provider, "_model", None) or "diagnostic"
    report = {
        "diagnostic": "structured_extraction_v1",
        "source": "synthetic_cases_only",
        "provider": {
            "name": getattr(provider, "provider_name", None) or getattr(provider, "served_provider", None)
                    or provider.__class__.__name__,
            "model": model,
            "configured": not isinstance(provider, SyntheticProvider),
        },
        "provider_contract": {
            "required": ["LLMProvider.complete", "Completion.input_tokens", "Completion.output_tokens"],
            "optional": ["Completion.rate_limits (allowlisted metadata only)"],
            "native_schema": "requires provider support for json_schema; prompt fallback remains separately measurable",
        },
        "cases": {},
        # Explicit invariants make it reviewable that this runner cannot publish graph
        # data even when a caller injects a provider.
        "writes": {"graph_revision": 0, "fact_publication": 0},
    }
    for case_id, source, intent in CASES:
        rows = [{"id": f"p{i}", "text": text} for i, text in enumerate(source.split("\n"))]
        case = report["cases"].setdefault(case_id, {"intent": intent, "variants": {}})
        for transport in ("duplicated", "native"):
            # ``transport`` describes the caller's prompt variant; ``effective``
            # describes how the provider transports the schema argument in either
            # variant. This keeps duplicated mode measurable as prompt copy plus the
            # adapter's actual native or fallback copy.
            effective = effective_schema_transport(provider, model)
            prompt_schema = json.dumps(SCHEMA, ensure_ascii=False) if transport == "duplicated" else ""
            input_data = {"case_id": case_id, "source": source, "passages": rows}
            prompt = ("Extract only source-supported claims.\n" +
                      ("OUTPUT JSON SCHEMA:\n" + prompt_schema + "\n" if prompt_schema else "") +
                      "INPUT DATA:\n" + json.dumps(input_data, ensure_ascii=False))
            batches = pack_passages(rows, system="stable diagnostic instructions",
                                    instructions="Extract only source-supported claims.\nINPUT DATA:\n",
                                    input_fields={"case_id": case_id, "source": source},
                                    schema=SCHEMA, schema_transport=(
                                        "duplicated" if transport == "duplicated" else effective),
                                    context_tokens=context_tokens, output_tokens=output_tokens)
            started = asyncio.get_running_loop().time()
            try:
                completion = await provider.complete(prompt, system="stable diagnostic instructions",
                                                     json_mode=True, cls=Class.BATCH,
                                                     model=model, json_schema=SCHEMA,
                                                     max_output_tokens=output_tokens)
                try:
                    extracted = json.loads(completion.text)
                    valid, error = _validate(extracted, SCHEMA, rows, case_id)
                except (TypeError, ValueError) as exc:
                    extracted, valid, error = None, False, type(exc).__name__
                # The provider receives these exact protocol fields.  Report their
                # individual byte/token contributions so request sizing remains
                # reproducible even when the adapter adds fallback schema guidance.
                system_text = "stable diagnostic instructions"
                fallback_schema = (json.dumps(SCHEMA, ensure_ascii=False)
                                   if effective == "prompt" else "")
                wire_schema_text = (json.dumps(SCHEMA, ensure_ascii=False)
                                    if effective == "native" else "")
                request_tokens = (conservative_token_estimate(system_text) +
                                  conservative_token_estimate(prompt) +
                                  conservative_token_estimate(fallback_schema) +
                                  conservative_token_estimate(wire_schema_text))
                item = {"status": "completed" if valid else "invalid",
                        "elapsed_s": asyncio.get_running_loop().time() - started,
                        "request": {"tokens": request_tokens,
                                    "output_headroom": output_tokens,
                                    "bytes": (len(system_text.encode()) + len(prompt.encode()) +
                                              len(fallback_schema.encode()) + len(wire_schema_text.encode())),
                                    "schema_transport": transport,
                                    "effective_transport": effective,
                                    "system_tokens": conservative_token_estimate(system_text + fallback_schema),
                                    "prompt_tokens": conservative_token_estimate(prompt),
                                    "schema_tokens": conservative_token_estimate(wire_schema_text)},
                        "usage": {"input_tokens": completion.input_tokens,
                                  "output_tokens": completion.output_tokens,
                                  "cache_read_tokens": completion.cache_read_tokens,
                                  "cache_write_tokens": completion.cache_write_tokens},
                        "served_provider": completion.served_provider,
                        "served_model": completion.served_model,
                        **({"rate_limit": _safe_rate_limits(completion)}
                           if _safe_rate_limits(completion) is not None else {}),
                        "validation": {"valid": valid, "error": error},
                        "extracted_json": extracted}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                item = _error_metadata(exc)
            case["variants"][transport] = item
    return report


def _configured_provider(name: str, model: str | None, base_url: str | None) -> LLMProvider:
    """Build an explicitly requested hosted provider; never called by default."""
    if name != "groq":
        raise ValueError("live diagnostics currently support --provider groq; omit it for synthetic mode")
    from novel_llm import GroqProvider
    import os
    return GroqProvider(model=model or os.environ.get("LLM_MODEL_EXTRACT", "llama-3.3-70b-versatile"),
                        base_url=base_url or os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                        api_key=os.environ.get("GROQ_API_KEY"), max_output_tokens=2048)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline synthetic extraction diagnostic")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--context-tokens", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=256)
    parser.add_argument("--provider", choices=["synthetic", "groq"], default="synthetic",
                        help="explicitly opt into Groq transport using synthetic cases only")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    provider = None if args.provider == "synthetic" else _configured_provider(
        args.provider, args.model, args.base_url)
    try:
        report = asyncio.run(run_diagnostic(provider, context_tokens=args.context_tokens,
                                            output_tokens=args.output_tokens, model=args.model))
    finally:
        if provider is not None:
            close = getattr(provider, "aclose", None)
            if close is not None:
                asyncio.run(close())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
