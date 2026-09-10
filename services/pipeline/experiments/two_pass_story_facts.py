#!/usr/bin/env python3
"""Read-only experiment: broad story analysis, then evidence-grounded JSON.

This deliberately does not import the pipeline worker, open Postgres, or write graph
rows.  It tests a possible replacement/companion for STATE EXTRACT before that design
is allowed near the append-only knowledge path (instructions.md §0.2, §5 step 7).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import hashlib
import re
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from novel_llm import Class, OllamaProvider
from pydantic import BaseModel, ConfigDict, Field
from pipeline.knowledge_contract import unique_json_object


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityLink(StrictModel):
    surface: str = Field(min_length=1, max_length=100)
    role: str = Field(
        min_length=1,
        max_length=80,
        description="The entity's role in this claim, such as target, ally, rival, or cause.",
    )


class EvidenceRef(StrictModel):
    passage_id: str = Field(min_length=1)


class StoryClaim(StrictModel):
    claim_id: str = Field(pattern=r"^c[1-9][0-9]*$")
    kind: str = Field(
        min_length=1,
        max_length=80,
        description=(
            "A concise open-vocabulary snake_case category, e.g. life_status, "
            "relationship_attitude, conflict, personality_trait, appearance, or development."
        ),
    )
    subject: str = Field(min_length=1, max_length=100)
    predicate: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=300)
    linked_entities: list[EntityLink] = Field(max_length=12)
    epistemic_status: Literal["explicit", "strongly_implied", "ambiguous"]
    temporal_status: Literal[
        "current", "began", "ended", "historical", "instantaneous", "ongoing", "unclear"
    ]
    importance: Literal["crucial", "significant", "characterization"]
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=3)
    rationale: str = Field(min_length=1, max_length=240)


class StoryClaims(StrictModel):
    claims: list[StoryClaim] = Field(max_length=64)


ANALYST_SYSTEM = """\
You are a meticulous story analyst building durable memory for a serialized novel.
Read only the supplied chapter. Discover the story knowledge a future reader would care
about; do not force it into a database schema yet.

Seek high-value facts of ANY genre, including but not limited to:
- life and state changes: injury, death, disappearance, resurrection, transformation;
- relationships and attitudes: affection, trust, loyalty, hatred, fear, rivalry, betrayal;
- active conflict: who opposes whom, what caused it, what each side wants, and its outcome;
- stable descriptions: appearance, abilities, limitations, possessions, roles, affiliations;
- personality revealed by repeated or diagnostic choices, not generic momentary behavior;
- character development: a meaningful change from an earlier attitude, value, goal, or habit;
- goals, decisions, discoveries, secrets, constraints, and consequential events.

Be sensitive to implication but calibrate it. A blush, jealousy, unusual concern, or intimate
gesture can be evidence of attraction; it is not automatically proof of love. A threat is not
a completed attack. A reported death is not confirmed death. A moment of anger is not a stable
personality trait. Preserve negation, uncertainty, attribution, cause, and whether an attempt
succeeded. Prefer atomic claims; do not combine several independently falsifiable facts.

Write plain-text candidate cards, not JSON. For each candidate give:
CANDIDATE, SUBJECT, POSSIBLE KIND, CLAIM, OTHER ENTITIES/ROLES, CERTAINTY,
TEMPORAL READING, WHY IT MATTERS, and EVIDENCE (passage ID plus an exact excerpt).
Omit scenery and routine micro-actions unless they establish a durable trait, relationship,
state change, cause, goal, or plot consequence. It is correct to return no candidates.
"""


COMPILER_SYSTEM = """\
You are the conservative compiler and fact-checker in a two-pass story extraction experiment.
The analyst notes are untrusted proposals, not evidence. Re-read the offered source passages.
Return only atomic claims that the source supports explicitly or by a strong, narratively
conventional implication. Drop speculation and duplicates. Do not add a claim merely to cover
a category.

The `kind` field is open vocabulary: choose the most precise concise snake_case label rather
than fitting a fixed ontology. `subject` and linked entity surfaces must use names exactly as
written in the source. Use linked_entities when the claim semantically connects the subject to
another named entity; do not use it for incidental co-presence.

Use `strongly_implied` only when the text supplies a concrete behavioral or emotional cue and
the inference is the ordinary narrative reading. Use `ambiguous` when multiple live readings
remain; phrase `value` as the observed signal rather than promoting one reading to fact.
Character development requires evidence of change, not merely a trait shown once.

Every claim must cite one to three offered passage IDs. The application, not you, will copy
the passage text and offsets into the evidence record. `subject` must be one named entity,
never a composite such as "A vs B"; represent the other participants in linked_entities.
Preserve qualifications, attribution, causality, attempted/prevented/completed status, and
temporal scope. Never treat the analyst's wording as source evidence.
"""

# Experiment vocabulary only; production ontology remains per-novel data (§0.4).
DEFAULT_KINDS = ["life_status", "relationship_attitude", "conflict", "personality_trait",
                 "appearance", "development", "ability", "possession", "affiliation",
                 "goal", "decision", "discovery", "event", "other"]
FIELD_GUIDANCE = """
Write predicate, value, rationale and link roles in English; retain exact source names
in subject and surface (and when referring to named entities in English sentences).
predicate is a short snake_case attribute or relation (e.g. injury_status or distrusts).
value is its object/complement (e.g. right arm corroded by rain), not a repeated predicate.
kind must be an offered category, never a schema key such as subject.
linked_entities excludes the subject itself. role describes the other participant's
semantic role, e.g. target, cause, ally, opponent, owner; never subject or linked_entity.
epistemic_status: explicit = directly stated; strongly_implied = inferred from a concrete
cue; ambiguous = unresolved competing readings. Directly narrated injuries are explicit.
temporal_status: current = state at chapter end; began/ended = state transition;
historical = prior event; instantaneous = momentary event; ongoing = continuing action;
unclear = timing cannot be determined.
importance: crucial = major plot/state consequence; significant = durable useful fact;
characterization = descriptive characterization. Choose independently for each claim.
Do not manufacture variety; uniform labels are acceptable only when supported.
Each slot holds at most one atomic claim from its assigned candidate, or null to drop it.
Do not copy illustrative examples as facts. Return only the specified JSON object.
"""

MINIMAL_SYSTEM = """仅从给定章节原文提取简短事实。分析笔记只是待核查的候选，不是证据。
每个候选槽位最多输出一条中文原子陈述和支持它的段落ID；无可靠事实则输出 null。
陈述应保留原文人物名称、否定、时间和不确定性。只保留原文支持的部分，去掉推测。
不要把一时情绪变成持久性格，不要把计划当作完成，不要混淆谁知道什么。
不做实体链接、分类、评分、英文翻译或解释。不要为了填满槽位编造事实。
仅返回指定 JSON 实例。"""


def minimal_schema(refs, rows):
    item = {"type": "object", "additionalProperties": False,
            "required": ["statement", "passage_ids"], "properties": {
                "statement": {"type": "string", "minLength": 1, "maxLength": 180},
                "passage_ids": {"type": "array", "minItems": 1, "maxItems": 3,
                                "items": {"type": "string", "enum": [r["id"] for r in rows]}}}}
    return {"type": "object", "additionalProperties": False, "required": refs,
            "properties": {ref: {"anyOf": [item, {"type": "null"}]} for ref in refs}}


def materialize_minimal(raw, refs, rows):
    body = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(body, dict) or set(body) != set(refs):
        raise ValueError("Invalid minimal slots")
    by_id = {row["id"]: row for row in rows}
    results = []
    for ref in refs:
        item = body[ref]
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {"statement", "passage_ids"}:
            raise ValueError("Invalid minimal fields")
        statement, ids = item["statement"], item["passage_ids"]
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 180:
            raise ValueError("Invalid statement")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 3 or
                any(not isinstance(p, str) or p not in by_id for p in ids)):
            raise ValueError("Invalid evidence references")
        # Application-owned source evidence, never graph identity (§0.2, §0.3).
        results.append({"candidate_id": ref, "statement": statement,
                        "evidence": [by_id[p] for p in ids], "review_required": True})
    return results


def candidate_cards(analysis: str) -> list[str]:
    starts = list(re.finditer(r"(?m)^[ \t]*(?:\*\*)?CANDIDATE(?:[_ #]+\d+|:)", analysis))
    if not starts:
        raise ValueError("No CANDIDATE cards found; cannot silently truncate analyst notes")
    return [analysis[m.start():starts[i + 1].start() if i + 1 < len(starts) else len(analysis)]
            for i, m in enumerate(starts)]


def slot_schema(refs: list[str], rows: list[dict], kinds: list[str]) -> dict:
    """Inline nullable slots, as in knowledge_contract; no invisible $ref definitions."""
    schema = StoryClaim.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, list):
            return [inline(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return inline(definitions[node["$ref"].split("/")[-1]])
        return {k: inline(v) for k, v in node.items() if k != "title"}

    item = inline(schema)
    props = item["properties"]
    props.pop("claim_id")
    item["required"].remove("claim_id")
    props["kind"] = {"type": "string", "enum": kinds}
    props["predicate"] = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,47}$"}
    props["value"]["maxLength"] = 180
    props["rationale"]["maxLength"] = 160
    props["linked_entities"]["maxItems"] = 4
    props["evidence"]["items"]["properties"]["passage_id"] = {
        "type": "string", "enum": [row["id"] for row in rows]}
    return {"type": "object", "additionalProperties": False, "required": refs,
            "properties": {ref: {"anyOf": [item, {"type": "null"}]} for ref in refs}}


def materialize_slots(raw: str, refs: list[str], schema: dict) -> StoryClaims:
    body = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(body, dict) or set(body) != set(refs):
        raise ValueError("Compiler must return exactly the offered slots")
    for item in body.values():
        if item is None:
            continue
        props = schema["properties"][refs[0]]["anyOf"][0]["properties"]
        if not isinstance(item, dict) or set(item) != set(props):
            raise ValueError("Invalid slot fields")
        if item["kind"] not in props["kind"]["enum"]:
            raise ValueError("Unoffered kind")
        if not isinstance(item["predicate"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", item["predicate"]):
            raise ValueError("Invalid compact predicate")
        if len(item["value"]) > 180 or len(item["rationale"]) > 160 or len(item["linked_entities"]) > 4:
            raise ValueError("Slot exceeds output bounds")
    return StoryClaims(claims=[dict(claim_id=ref, **body[ref]) for ref in refs
                               if body[ref] is not None])


def quality_report(claims: list[dict]) -> dict:
    """Heuristics are warnings, not an entailment verifier or proof of quality."""
    fields = ("kind", "epistemic_status", "temporal_status", "importance")
    distributions = {key: dict(Counter(c[key] for c in claims)) for key in fields}
    issues = []
    cjk_fields = Counter()
    for c in claims:
        reasons = []
        if c["kind"] in {"subject", "kind", "linked_entity"}:
            reasons.append("schema vocabulary in kind")
        if any(l["role"] in {"subject", "linked_entity", "surface"}
               or l["surface"] == c["subject"] for l in c["linked_entities"]):
            reasons.append("invalid semantic link role or self-link")
        if c["predicate"].strip().casefold() == c["value"].strip().casefold():
            reasons.append("predicate duplicates value")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", c["predicate"]):
            reasons.append("predicate is not a compact English relation")
        names = sorted({c["subject"], *(l["surface"] for l in c["linked_entities"])}, key=len, reverse=True)
        for field in ("predicate", "value", "rationale"):
            prose = c[field]
            for name in names:
                prose = prose.replace(name, "")
            if re.search(r"[\u3400-\u9fff]", prose):
                cjk_fields[field] += 1
        if reasons:
            issues.append({"claim_id": c["claim_id"], "reasons": reasons})
    constant = [key for key, dist in distributions.items() if len(dist) == 1 and len(claims) >= 5]
    return {"distributions": distributions, "constant_fields": constant, "claim_issues": issues,
            "cjk_prose_claim_counts": dict(cjk_fields),
            "needs_review": bool(issues or constant or cjk_fields), "semantic_support_verified": False}


def passages(text: str) -> list[dict]:
    """Paragraph-sized, application-owned evidence references with source offsets."""
    result: list[dict] = []
    cursor = 0
    for block in text.splitlines(keepends=True):
        body = block.rstrip("\r\n")
        start = cursor
        cursor += len(block)
        if not body.strip():
            continue
        leading = len(body) - len(body.lstrip())
        trailing = len(body.rstrip())
        result.append(
            {
                "id": f"p{len(result) + 1}",
                "char_start": start + leading,
                "char_end": start + trailing,
                "text": body[leading:trailing],
            }
        )
    if not result and text.strip():
        start = len(text) - len(text.lstrip())
        body = text.strip()
        result.append({"id": "p1", "char_start": start, "char_end": start + len(body), "text": body})
    return result


def format_passages(rows: list[dict]) -> str:
    return "\n".join(f'<passage id="{row["id"]}">{row["text"]}</passage>' for row in rows)


def analyst_prompt(rows: list[dict]) -> str:
    return "<source_passages>\n" + format_passages(rows) + "\n</source_passages>"


def compiler_prompt(rows: list[dict], analysis: str) -> str:
    # Source is repeated intentionally: pass 2 verifies pass 1 instead of laundering it.
    return (
        "<source_passages>\n"
        + format_passages(rows)
        + "\n</source_passages>\n<untrusted_analyst_notes>\n"
        + analysis
        + "\n</untrusted_analyst_notes>"
    )


def validate_evidence(claims: StoryClaims, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Materialize application-owned quotations and reject invented surfaces (§0.2)."""
    by_id = {row["id"]: row for row in rows}
    source = "\n".join(row["text"] for row in rows)
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_ids: set[str] = set()
    for claim in claims.claims:
        dumped = claim.model_dump()
        reason = None
        if claim.claim_id in seen_ids:
            reason = "duplicate claim_id"
        seen_ids.add(claim.claim_id)
        if claim.subject not in source:
            reason = "subject is not an exact source surface"
        if any(link.surface not in source for link in claim.linked_entities):
            reason = "linked entity is not an exact source surface"
        materialized = []
        for evidence in claim.evidence:
            passage = by_id.get(evidence.passage_id)
            if passage is None:
                reason = f"unknown passage {evidence.passage_id}"
                break
            materialized.append(
                {
                    **evidence.model_dump(),
                    "quote": passage["text"],
                    "char_start": passage["char_start"],
                    "char_end": passage["char_end"],
                }
            )
        if reason:
            rejected.append({"claim": dumped, "rejection": reason})
        else:
            accepted.append({**dumped, "evidence": materialized})
    return accepted, rejected


def load_chapter(path: Path, chapter: int, text_field: str = "source") -> str:
    if path.suffix.lower() != ".json":
        return path.read_text()
    document = json.loads(path.read_text())
    for row in document.get("chapters", []):
        if row.get("chapter") == chapter:
            return row[text_field]
    raise ValueError(f"chapter {chapter} is absent from {path}")


async def run(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    if args.source_only and (not args.minimal or args.replay_analysis):
        raise ValueError("--source-only requires --minimal and cannot replay analyst notes")
    source = load_chapter(args.input, args.chapter, args.text_field)
    rows = passages(source)
    provider = OllamaProvider(
        host=args.host,
        model=args.model,
        timeout=args.idle_timeout,
        first_token_timeout=args.first_token_timeout,
        total_timeout=args.total_timeout,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        stream=True,
        think=False,
    )
    # Keep sampling identical in the grammar/no-grammar diagnostic. Local harness only.
    provider._options["temperature"] = 0
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    if args.source_only:
        item = minimal_schema(["c1"], rows)["properties"]["c1"]["anyOf"][0]
        schema = {"type": "object", "additionalProperties": False, "required": ["statements"],
                  "properties": {"statements": {"type": "array", "maxItems": 16, "items": item}}}
        prompt = (analyst_prompt(rows) + "\n直接从原文选择最多16条独立的重要事实，不重复。空余槽位填null。\n" +
                  "JSON schema:\n" + json.dumps(schema, ensure_ascii=False))
        prompt = prompt.replace("空余槽位填null。", "没有事实时返回空数组。")
        system = "从章节原文提取最多16条中文原子事实，每条仅包含statement和支持它的passage_ids。保留原文人物、否定、时间和不确定性。不要推测、分类、链接、翻译或解释。只返回JSON实例。"
        second = await provider.complete(prompt, system=system, cls=Class.BATCH,
                                         model=args.model, json_schema=schema if args.decoding == "schema" else None)
        checkpoint = {"experiment": "minimal_source_only_v1", "status": "incomplete",
                      "input": {"path": str(args.input.resolve()), "chapter": args.chapter,
                                "text_field": args.text_field, "text_sha256": source_hash},
                      "model": {"requested": args.model, "served": second.served_model},
                      "settings": {"decoding": args.decoding, "max_statements": 16, "shape": "array"},
                      "raw": second.text, "graph_ready": False,
                      "usage": {"calls": 1, "input_tokens": second.input_tokens,
                                "output_tokens": second.output_tokens}}
        if args.output:
            args.output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")
        body = json.loads(second.text, object_pairs_hook=unique_json_object)
        if (not isinstance(body, dict) or set(body) != {"statements"} or
                not isinstance(body["statements"], list) or len(body["statements"]) > 16 or
                any(not isinstance(item, dict) for item in body["statements"])):
            raise ValueError("Invalid minimal statement array")
        slots = {f"c{i+1}": item for i, item in enumerate(body["statements"])}
        statements = materialize_minimal(json.dumps(slots), list(slots), rows)
        return {**checkpoint, "status": "completed", "statements": statements,
                "seconds": round(time.monotonic() - started, 3)}
    if args.replay_analysis:
        saved = json.loads(args.replay_analysis.read_text())
        original = saved["input"]
        expected_hash = original.get("text_sha256")
        if expected_hash is None:
            expected_hash = hashlib.sha256(load_chapter(Path(original["path"]), original["chapter"]).encode()).hexdigest()
        if expected_hash != source_hash:
            raise ValueError("Replayed analysis belongs to different input text")
        analysis = saved["analysis"]
        analysis_model = saved["model"]["analysis_served"]
        analysis_usage = {"replayed": True}
    else:
        first = await provider.complete(
            analyst_prompt(rows), system=ANALYST_SYSTEM, cls=Class.BATCH, model=args.model)
        analysis, analysis_model = first.text, first.served_model
        analysis_usage = {"input_tokens": first.input_tokens, "output_tokens": first.output_tokens}
    cards = candidate_cards(analysis)
    compiled, batches = [], []
    checkpoint = {"status": "incomplete", "analysis": analysis, "batches": batches,
                  "input": {"path": str(args.input.resolve()), "chapter": args.chapter,
                            "text_field": args.text_field, "text_sha256": source_hash},
                  "model": {"requested": args.model, "analysis_served": analysis_model},
                  "decoding": args.decoding}
    if args.output:
        args.output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")
    for start in range(0, len(cards), args.batch_size):
        batch = cards[start:start + args.batch_size]
        refs = [f"c{i + 1}" for i in range(start, start + len(batch))]
        schema = minimal_schema(refs, rows) if args.minimal else slot_schema(refs, rows, args.kinds)
        notes = "\n".join(f"Slot {ref}:\n{card}" for ref, card in zip(refs, batch))
        prompt = compiler_prompt(rows, notes) + ("" if args.minimal else "\n" + FIELD_GUIDANCE)
        prompt += "\nRequired output schema:\n" + json.dumps(schema, ensure_ascii=False)
        second = await provider.complete(
            prompt, system=MINIMAL_SYSTEM if args.minimal else COMPILER_SYSTEM.replace(
                "The `kind` field is open vocabulary: choose the most precise concise snake_case label rather\nthan fitting a fixed ontology.",
                "Choose `kind` from the categories supplied in the output schema."),
            cls=Class.BATCH, model=args.model,
            json_schema=schema if args.decoding == "schema" else None)
        batches.append({"refs": refs, "raw": second.text, "served_model": second.served_model,
                        "input_tokens": second.input_tokens, "output_tokens": second.output_tokens})
        if args.output:
            args.output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n")
        if args.minimal:
            compiled.extend(materialize_minimal(second.text, refs, rows))
        else:
            parsed = materialize_slots(second.text, refs, schema)
            compiled.extend(parsed.claims)
    if args.minimal:
        return {**checkpoint, "experiment": "minimal_unlinked_statements_v1", "status": "completed",
                "settings": {"batch_size": args.batch_size, "decoding": args.decoding},
                "usage": {"analysis": analysis_usage, "compiler_calls": len(batches),
                          "compiler_output_tokens": sum(b["output_tokens"] for b in batches)},
                "seconds": round(time.monotonic() - started, 3),
                "candidate_count": len(cards), "statements": compiled,
                "dropped_candidate_ids": [f"c{i+1}" for i in range(len(cards))
                                          if f"c{i+1}" not in {c["candidate_id"] for c in compiled}],
                "graph_ready": False}
    parsed = StoryClaims(claims=compiled)
    accepted, rejected = validate_evidence(parsed, rows)
    quality = quality_report([claim.model_dump() for claim in parsed.claims])
    flagged = {issue["claim_id"] for issue in quality["claim_issues"]}
    return {
        "experiment": "two_pass_story_facts_v3_keyed",
        "status": "completed",
        "input": {"path": str(args.input.resolve()), "chapter": args.chapter, "source_chars": len(source),
                  "text_field": args.text_field, "text_sha256": source_hash},
        "settings": {"decoding": args.decoding, "kinds": args.kinds, "batch_size": args.batch_size},
        "model": {
            "requested": args.model,
            "analysis_served": analysis_model,
            "compiler_served": [b["served_model"] for b in batches],
        },
        "usage": {
            "analysis": analysis_usage,
            "compiler": batches,
        },
        "analysis": analysis,
        "quality": quality,
        "quality_rejected_claims": [claim for claim in accepted if claim["claim_id"] in flagged],
        "contract_clean_claims": [claim for claim in accepted if claim["claim_id"] not in flagged],
        "graph_ready": False,  # No independent semantic verification in this experiment (§0.2).
        "claims": accepted,
        "rejected_claims": rejected,
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=root / "eval/knowledge/book-reviewed.json")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--minimal", action="store_true", help="Chinese statement + evidence only; no links or classifications")
    p.add_argument("--source-only", action="store_true", help="One minimal extraction call directly on source, without analyst notes")
    p.add_argument("--text-field", default="source", help="Explicit dataset field; use a saved translation for an English probe")
    p.add_argument("--replay-analysis", type=Path, help="Reuse saved pass 1 on identical input")
    p.add_argument("--batch-size", type=int, choices=range(1, 5), default=3)
    p.add_argument("--kinds", nargs="+", default=DEFAULT_KINDS)
    p.add_argument("--decoding", choices=["schema", "prompt"], default="schema")
    p.add_argument("--output", type=Path)
    p.add_argument("--host", default=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"))
    p.add_argument("--model", default=os.getenv("LLM_MODEL_EXTRACT", "qwen2.5:7b-instruct"))
    p.add_argument("--num-ctx", type=int, default=16_384)
    p.add_argument("--num-predict", type=int, default=4_096)
    p.add_argument("--first-token-timeout", type=float, default=900)
    p.add_argument("--idle-timeout", type=float, default=120)
    p.add_argument("--total-timeout", type=float, default=1_800)
    return p


def main() -> None:
    args = parser().parse_args()
    result = asyncio.run(run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
