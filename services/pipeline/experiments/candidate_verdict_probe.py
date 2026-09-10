"""Read-only single-candidate semantic probe; no graph IDs or publication (§0.2)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from novel_llm import Class, OllamaProvider
from pipeline.knowledge_contract import unique_json_object
from two_pass_story_facts import load_chapter, passages, format_passages


SYSTEM = """核查一条小说事实候选。章节原文是唯一证据，候选不是证据。
先判断整个候选是否有充分支持，包括人物、否定、时间、因果和确定性。
不能把不同时间的决定混为一谈，不能把读者知道的事当作人物知道的事。
一次情绪表现不足以证明持久性格转变。不能用修正候选来代替拒绝错误候选。
不支持就 supported=false，fact=null；支持才生成一条简短原子事实。
value 和 reason 用中文原文语言，不做英文翻译。predicate 用简短英文 snake_case。
subject 已由应用选定，无需输出姓名。links 的键是本次请求的实体选项，非数据库ID。
每个其他实体的值为它在该事实中的语义角色；无关就 null。不得为了填满槽位添加关系。
只输出 JSON 实例，不输出 schema 本身。不要附加解释或 Markdown。
"""
ROLES = ["target", "cause", "ally", "opponent", "group", "owner"]

VERDICT_SYSTEM = """只核查给定的完整候选陈述是否被本章原文支持，不改写陈述，不提取关系。
核对主语、宾语、否定、时间、因果及人物知情范围。部分正确不能算整条正确。
一次情绪表现不证明持久性格转变。读者知道的事情不等于人物知道。
supported 为布尔值；passage_ids 引用支持判断的原文段落；reason 用中文简短解释。
只输出 JSON 实例，不能输出 schema 定义。"""


def verdict_contract(rows):
    return {"type": "object", "additionalProperties": False,
            "required": ["supported", "passage_ids", "reason"], "properties": {
                "supported": {"type": "boolean"},
                "passage_ids": {"type": "array", "minItems": 1, "maxItems": 3,
                                "items": {"type": "string", "enum": [r["id"] for r in rows]}},
                "reason": {"type": "string", "minLength": 1, "maxLength": 180}}}


def validate_verdict(raw, rows):
    body = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(body, dict) or set(body) != {"supported", "passage_ids", "reason"}:
        raise ValueError("Invalid verdict fields")
    if type(body["supported"]) is not bool or not isinstance(body["reason"], str) or not 1 <= len(body["reason"]) <= 180:
        raise ValueError("Invalid verdict or reason")
    ids = body["passage_ids"]
    offered = {r["id"] for r in rows}
    if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(ref, str) or ref not in offered for ref in ids):
        raise ValueError("Unoffered evidence")
    return body


def contract(subject: str, entities: dict[str, str]) -> dict:
    links = {ref: {"anyOf": [{"type": "string", "enum": ROLES}, {"type": "null"}]}
             for ref, surface in entities.items() if surface != subject}
    fact = {"type": "object", "additionalProperties": False,
            "required": ["predicate", "value", "passage_ids", "links"], "properties": {
                "predicate": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,47}$"},
                "value": {"type": "string", "minLength": 1, "maxLength": 160},
                "passage_ids": {"type": "array", "minItems": 1, "maxItems": 3,
                                "items": {"type": "string"}},
                "links": {"type": "object", "additionalProperties": False,
                          "required": list(links), "properties": links}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["supported", "reason", "fact"], "properties": {
                "supported": {"type": "boolean"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 180},
                "fact": {"anyOf": [fact, {"type": "null"}]}}}


def validate(raw, subject, entities, rows):
    import re
    body = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(body, dict) or set(body) != {"supported", "reason", "fact"}:
        raise ValueError("Unexpected verdict fields")
    if type(body["supported"]) is not bool or not isinstance(body["reason"], str) or not 1 <= len(body["reason"]) <= 180:
        raise ValueError("Invalid verdict or reason")
    fact = body["fact"]
    if not body["supported"]:
        if fact is not None:
            raise ValueError("Rejected candidate must have null fact")
        return body
    if not isinstance(fact, dict) or set(fact) != {"predicate", "value", "passage_ids", "links"}:
        raise ValueError("Supported candidate requires complete fact")
    if not isinstance(fact["predicate"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", fact["predicate"]):
        raise ValueError("Invalid predicate")
    if not isinstance(fact["value"], str) or not 1 <= len(fact["value"]) <= 160:
        raise ValueError("Invalid value")
    by_id = {row["id"]: row for row in rows}
    ids = fact["passage_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(ref, str) or ref not in by_id for ref in ids):
        raise ValueError("Unoffered evidence")
    expected = {ref for ref, surface in entities.items() if surface != subject}
    links = fact["links"]
    if not isinstance(links, dict) or set(links) != expected:
        raise ValueError("Unoffered link or self-link")
    if any(role is not None and role not in ROLES for role in links.values()):
        raise ValueError("Unoffered semantic role")
    # Request-local refs never resolve graph identity (§0.3); preserve provenance only.
    body["materialized"] = {"subject": subject, "predicate": fact["predicate"],
                            "value": fact["value"],
                            "linked_entities": [{"surface": entities[ref], "role": role}
                                                for ref, role in links.items() if role is not None],
                            "evidence": [by_id[ref] for ref in ids]}
    return body


def score(results):
    tp = sum(r.get("verdict", {}).get("supported") is True and r["expected_supported"] for r in results)
    fp = sum(r.get("verdict", {}).get("supported") is True and not r["expected_supported"] for r in results)
    tn = sum(r.get("verdict", {}).get("supported") is False and not r["expected_supported"] for r in results)
    fn = sum(r.get("verdict", {}).get("supported") is False and r["expected_supported"] for r in results)
    errors = sum("error" in r for r in results)
    return dict(true_positive=tp, false_positive=fp, true_negative=tn, false_negative=fn,
                errors=errors, correct=tp + tn, total=len(results),
                note="Verdict agreement only; generated values and links require separate review.")


async def run(args):
    fixture = json.loads(args.cases.read_text())
    source = load_chapter(args.input, args.chapter)
    if hashlib.sha256(source.encode()).hexdigest() != fixture["source_sha256"]:
        raise ValueError("Fixture belongs to a different chapter text")
    entities = {f"e{i+1}": name for i, name in enumerate(fixture["entities"])}
    if len(set(entities.values())) != len(entities) or any(name not in source for name in entities.values()):
        raise ValueError("Entity options must be unique exact source surfaces")
    if any(case["subject"] not in entities.values() for case in fixture["cases"]):
        raise ValueError("Unoffered subject")
    rows = passages(source)
    provider = OllamaProvider(host=args.host, model=args.model, num_ctx=16384, num_predict=4096,
                              stream=True, think=False, first_token_timeout=60,
                              timeout=40, total_timeout=90)
    provider._options["temperature"] = 0
    result = {"experiment": "single_candidate_source_v1", "status": "incomplete",
              "verdict_only": args.verdict_only,
              "source_sha256": fixture["source_sha256"], "reviewed_by": fixture["reviewed_by"],
              "model": args.model, "graph_ready": False, "results": []}
    start = time.monotonic()
    try:
        for case in fixture["cases"]:
            schema = contract(case["subject"], entities)
            schema["properties"]["fact"]["anyOf"][0]["properties"]["passage_ids"]["items"]["enum"] = [r["id"] for r in rows]
            # Expected labels/review comments never enter the request.
            prompt = ("<source>\n" + format_passages(rows) + "\n</source>\n" +
                      json.dumps({"subject": case["subject"], "candidate": case["proposal"],
                                  "entity_options": entities}, ensure_ascii=False) +
                      "\n输出结构定义（输出实例，不要复制定义）：\n" + json.dumps(schema, ensure_ascii=False))
            if args.verdict_only:
                schema = verdict_contract(rows)
                prompt = ("<source>\n" + format_passages(rows) + "\n</source>\n" +
                          "候选主语：" + case["subject"] + "\n候选陈述：" + case["subject"] + "：" + case["proposal"] +
                          "\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False))
            record = dict(case)
            call_start = time.monotonic()
            try:
                completion = await provider.complete(prompt, system=VERDICT_SYSTEM if args.verdict_only else SYSTEM, cls=Class.BATCH,
                                                     model=args.model, json_schema=schema)
                record.update(raw=completion.text, served_model=completion.served_model,
                              input_tokens=completion.input_tokens, output_tokens=completion.output_tokens)
                record["verdict"] = (validate_verdict(completion.text, rows) if args.verdict_only else
                                     validate(completion.text, case["subject"], entities, rows))
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            record["seconds"] = round(time.monotonic() - call_start, 3)
            result["results"].append(record)
            result["score"] = score(result["results"])
            result["seconds"] = round(time.monotonic() - start, 3)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"case": case["id"], "seconds": record["seconds"],
                              "supported": record.get("verdict", {}).get("supported"),
                              "error": record.get("error")}), flush=True)
    finally:
        await provider._client.aclose()
    result["status"] = "completed"
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    root = Path(__file__).resolve().parents[3]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=root / "eval/knowledge/book-reviewed.json")
    p.add_argument("--cases", type=Path, default=root / "eval/knowledge/ling-candidate-checks.json")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--verdict-only", action="store_true")
    p.add_argument("--host", default="http://127.0.0.1:11436")
    p.add_argument("--model", default="maternion/ling-3.0-tiny:8b")
    p.add_argument("--output", type=Path, required=True)
    result = asyncio.run(run(p.parse_args()))
    print(json.dumps(result["score"]))


if __name__ == "__main__":
    main()
