"""Select immutable statement candidates; JSON serialization belongs to Python (§0.2).

This diagnostic supplies reviewed candidate alternatives. It does not solve candidate
discovery, identity resolution, or prove that a cited passage entails a statement.
"""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from novel_llm import Class, OllamaProvider
from pipeline.knowledge_contract import unique_json_object
from two_pass_story_facts import load_chapter, passages, format_passages

SYSTEM = """根据本章原文，为每道题选择唯一一条被完整支持的陈述。
逐项核对谁对谁做了什么、否定、知情范围和先后时间。不能只匹配出现过的名字。
选项均不成立或无法确定则选 none；不要求每题选出事实。
先选最直接的证据段落，再选答案；涉及前后变化时引用两个时点的段落。
只返回题目槽位的 JSON 实例。不改写事实，不输出解释，不产生新关系或新姓名。
证据和选项的顺序不代表答案；所有问题独立判断。"""


def request(cases, rows, reverse=False):
    questions, props, choices = {}, {}, {}
    for i, case in enumerate(cases):
        ref = f"q{i+1}"
        indices = list(range(len(case["options"])))
        if reverse:
            indices.reverse()
        choices[ref] = {f"s{j+1}": index for j, index in enumerate(indices)}
        questions[ref] = {key: case["options"][index] for key, index in choices[ref].items()}
        questions[ref]["none"] = "没有任何选项被完整支持，或无法确定"
        props[ref] = {"type": "object", "additionalProperties": False,
                      "required": ["a_evidence", "b_choice"], "properties": {
                          "a_evidence": {"type": "array", "maxItems": 3, "items": {
                              "type": "string", "enum": [r["id"] for r in rows]}},
                          "b_choice": {"type": "string", "enum": list(questions[ref])}}}
    schema = {"type": "object", "additionalProperties": False,
              "required": list(props), "properties": props}
    prompt = ("<source>\n" + format_passages(rows) + "\n</source>\n" +
              json.dumps({"questions": questions}, ensure_ascii=False) +
              "\n输出 JSON 结构定义：\n" + json.dumps(schema, ensure_ascii=False))
    return prompt, schema, choices


def materialize(raw, cases, choices, rows):
    body = json.loads(raw, object_pairs_hook=unique_json_object)
    if not isinstance(body, dict) or set(body) != set(choices):
        raise ValueError("Missing, extra or invalid question slots")
    by_id = {r["id"]: r for r in rows}
    results = []
    for ref, case in zip(choices, cases):
        item = body[ref]
        if not isinstance(item, dict) or set(item) != {"a_evidence", "b_choice"}:
            raise ValueError("Invalid decision fields")
        choice, evidence = item["b_choice"], item["a_evidence"]
        if not isinstance(choice, str) or choice not in {*choices[ref], "none"}:
            raise ValueError("Unoffered statement choice")
        if (not isinstance(evidence, list) or len(evidence) > 3 or
                any(not isinstance(e, str) or e not in by_id for e in evidence)):
            raise ValueError("Unoffered evidence")
        index = choices[ref].get(choice)
        if index is not None and not evidence:
            raise ValueError("Selected statement requires evidence")
        correct = index == case["expected_index"]
        required = case.get("evidence_all", [])
        alternatives = case.get("evidence_any", [])
        evidence_match = (set(required) <= set(evidence) and
                          (not alternatives or bool(set(alternatives) & set(evidence))))
        results.append({"case": case["id"], "selected_index": index, "correct": correct,
                        "reviewed_evidence_match": evidence_match,
                        "statement": case["options"][index] if index is not None else None,
                        "evidence": [by_id[e] for e in evidence]})
    return results


def consistency_report(calls, cases):
    """Quarantine order-sensitive decisions without consulting expected answers.

    Agreement is only a necessary check, never proof of entailment (§0.2).
    Even stable statements require separate semantic review.
    """
    decisions = {False: {}, True: {}}
    for call in calls:
        for result in call.get("results", []):
            decisions[call["reverse"]][result["case"]] = result
    report = []
    for case in cases:
        first, second = (decisions[order].get(case["id"]) for order in (False, True))
        if first is None or second is None:
            status = "quarantined_missing_valid_response"
        elif first["selected_index"] != second["selected_index"]:
            status = "quarantined_order_sensitive"
        elif first["selected_index"] is None:
            status = "abstained"
        else:
            status = "stable_but_unverified"
        report.append({"case": case["id"], "status": status, "graph_ready": False})
    return report


async def run(args):
    fixture = json.loads(args.cases.read_text())
    source = load_chapter(args.input, 1)
    if hashlib.sha256(source.encode()).hexdigest() != fixture["source_sha256"]:
        raise ValueError("Wrong source for fixture")
    rows = passages(source)
    provider = OllamaProvider(host=args.host, model=args.model, num_ctx=16384, num_predict=4096,
                              stream=True, think=False, first_token_timeout=60,
                              timeout=40, total_timeout=90)
    provider._options["temperature"] = 0
    output = {"experiment": "statement_choice_v1", "graph_ready": False,
              "decoding": args.decoding,
              "status": "incomplete", "model": args.model, "source_sha256": fixture["source_sha256"],
              "reviewed_by": fixture["reviewed_by"], "calls": []}
    started = time.monotonic()
    try:
        for reverse in (False, True):
            for start in range(0, len(fixture["cases"]), args.batch_size):
                cases = fixture["cases"][start:start + args.batch_size]
                prompt, schema, choices = request(cases, rows, reverse)
                record = {"reverse": reverse, "cases": [c["id"] for c in cases]}
                try:
                    completion = await provider.complete(prompt, system=SYSTEM, cls=Class.BATCH,
                                                         model=args.model, json_schema=schema if args.decoding == "schema" else None)
                    record.update(raw=completion.text, served_model=completion.served_model,
                                  input_tokens=completion.input_tokens, output_tokens=completion.output_tokens)
                    record["results"] = materialize(completion.text, cases, choices, rows)
                except Exception as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                output["calls"].append(record)
                args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        await provider._client.aclose()
    results = [r for call in output["calls"] for r in call.get("results", [])]
    output["consistency"] = consistency_report(output["calls"], fixture["cases"])
    output.update(status="completed", seconds=round(time.monotonic() - started, 3),
                  score={"expected_decisions": len(fixture["cases"]) * 2,
                         "valid_decisions": len(results), "correct": sum(r["correct"] for r in results),
                         "correct_with_reviewed_evidence": sum(r["correct"] and r["reviewed_evidence_match"] for r in results),
                         "call_errors": sum("error" in call for call in output["calls"])})
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return output


def main():
    root = Path(__file__).resolve().parents[3]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=root / "eval/knowledge/book-reviewed.json")
    p.add_argument("--cases", type=Path, default=root / "eval/knowledge/ling-statement-choices.json")
    p.add_argument("--host", default="http://127.0.0.1:11436")
    p.add_argument("--model", default="maternion/ling-3.0-tiny:8b")
    p.add_argument("--batch-size", type=int, choices=range(1, 5), default=3)
    p.add_argument("--decoding", choices=["schema", "prompt"], default="schema")
    p.add_argument("--output", type=Path, required=True)
    result = asyncio.run(run(p.parse_args()))
    print(json.dumps(result["score"]))


if __name__ == "__main__":
    main()
