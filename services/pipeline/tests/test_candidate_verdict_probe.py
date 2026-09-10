import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "experiments"))
    return importlib.import_module("candidate_verdict_probe")


def test_self_links_unknown_evidence_and_duplicate_keys_fail(monkeypatch):
    exp = probe(monkeypatch)
    entities = {"e1": "Alice", "e2": "Bob"}
    rows = exp.passages("Alice distrusts Bob.")
    body = {"supported": True, "reason": "Direct narration", "fact": {
        "predicate": "distrusts", "value": "Bob", "passage_ids": ["p1"],
        "links": {"e2": "target"}}}
    result = exp.validate(json.dumps(body), "Alice", entities, rows)
    assert result["materialized"]["linked_entities"] == [{"surface": "Bob", "role": "target"}]
    schema = exp.contract("Alice", entities)
    assert schema["properties"]["fact"]["anyOf"][0]["properties"]["links"]["required"] == ["e2"]
    body["fact"]["links"]["e1"] = "target"
    with pytest.raises(ValueError, match="self-link"):
        exp.validate(json.dumps(body), "Alice", entities, rows)
    body["fact"]["links"].pop("e1")
    body["fact"]["passage_ids"] = ["p999"]
    with pytest.raises(ValueError, match="evidence"):
        exp.validate(json.dumps(body), "Alice", entities, rows)
    with pytest.raises(ValueError, match="duplicate"):
        exp.validate('{"supported":false,"supported":true}', "Alice", entities, rows)


def test_score_does_not_reward_blanket_rejection_or_errors(monkeypatch):
    exp = probe(monkeypatch)
    score = exp.score([
        {"expected_supported": True, "verdict": {"supported": False}},
        {"expected_supported": False, "verdict": {"supported": False}},
        {"expected_supported": False, "error": "invalid JSON"}])
    assert score["correct"] == 1
    assert score["false_negative"] == 1
    assert score["errors"] == 1
    assert score["total"] == 3


def test_ten_requests_without_label_leakage(monkeypatch, tmp_path):
    exp = probe(monkeypatch)
    calls = []

    class Client:
        async def aclose(self):
            pass

    class Provider:
        def __init__(self, **kwargs):
            self._options = {}
            self._client = Client()

        async def complete(self, prompt, **kwargs):
            calls.append(prompt)
            return SimpleNamespace(text='{"supported":false,"reason":"unsupported","fact":null}',
                                   served_model="test", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(exp, "OllamaProvider", Provider)
    root = Path(__file__).parents[3]
    args = SimpleNamespace(cases=root / "eval/knowledge/ling-candidate-checks.json",
                           input=root / "eval/knowledge/book-reviewed.json", chapter=1,
                           host="unused", model="test", verdict_only=False, output=tmp_path / "result.json")
    result = asyncio.run(exp.run(args))
    assert len(calls) == 10
    assert all("expected_supported" not in p and '"review"' not in p for p in calls)
    assert result["score"]["true_negative"] == 5
    assert result["score"]["false_negative"] == 5
    assert result["graph_ready"] is False
    assert json.loads(args.output.read_text())["status"] == "completed"


def test_verdict_only_never_accepts_generated_fact_fields(monkeypatch):
    exp = probe(monkeypatch)
    rows = exp.passages("Alice left.")
    raw = '{"supported":true,"passage_ids":["p1"],"reason":"Directly stated"}'
    assert exp.validate_verdict(raw, rows)["supported"] is True
    body = json.loads(raw)
    body["fact"] = {"value": "invented"}
    with pytest.raises(ValueError, match="fields"):
        exp.validate_verdict(json.dumps(body), rows)
