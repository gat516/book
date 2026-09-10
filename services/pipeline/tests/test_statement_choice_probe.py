import importlib
import json
from pathlib import Path

import pytest


def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "experiments"))
    return importlib.import_module("statement_choice_probe")


def test_statement_is_materialized_and_order_does_not_change_identity(monkeypatch):
    exp = probe(monkeypatch)
    rows = exp.passages("Alice trusts Bob.")
    cases = [{"id": "trust", "options": ["Alice trusts Bob.", "Bob trusts Alice."],
              "expected_index": 0, "evidence_any": ["p1"]}]
    for reverse, choice in [(False, "s1"), (True, "s2")]:
        prompt, schema, choices = exp.request(cases, rows, reverse)
        assert "expected_index" not in prompt and "evidence_any" not in prompt
        assert "none" in schema["properties"]["q1"]["properties"]["b_choice"]["enum"]
        raw = json.dumps({"q1": {"a_evidence": ["p1"], "b_choice": choice}})
        result = exp.materialize(raw, cases, choices, rows)[0]
        assert result["statement"] == "Alice trusts Bob."
        assert result["correct"] and result["reviewed_evidence_match"]


def test_correct_choice_with_wrong_evidence_does_not_pass_evidence_metric(monkeypatch):
    exp = probe(monkeypatch)
    rows = exp.passages("Alice trusts Bob.\nBob leaves.")
    cases = [{"id": "trust", "options": ["Alice trusts Bob."], "expected_index": 0,
              "evidence_any": ["p1"]}]
    _, _, choices = exp.request(cases, rows)
    result = exp.materialize('{"q1":{"a_evidence":["p2"],"b_choice":"s1"}}', cases, choices, rows)[0]
    assert result["correct"] and not result["reviewed_evidence_match"]
    with pytest.raises(ValueError, match="requires evidence"):
        exp.materialize('{"q1":{"a_evidence":[],"b_choice":"s1"}}', cases, choices, rows)
    with pytest.raises(ValueError, match="Unoffered"):
        exp.materialize('{"q1":{"a_evidence":["p999"],"b_choice":"s1"}}', cases, choices, rows)


def test_abstention_and_invalid_output(monkeypatch):
    exp = probe(monkeypatch)
    rows = exp.passages("Alice trusts Bob.")
    cases = [{"id": "none", "options": ["Alice killed Bob."], "expected_index": None}]
    _, _, choices = exp.request(cases, rows)
    result = exp.materialize('{"q1":{"a_evidence":[],"b_choice":"none"}}', cases, choices, rows)[0]
    assert result["statement"] is None and result["correct"]
    with pytest.raises(ValueError, match="Invalid decision fields"):
        exp.materialize('{"q1":{"a_evidence":[],"b_choice":"none","statement":"invented"}}', cases, choices, rows)
    with pytest.raises(ValueError, match="duplicate"):
        exp.materialize('{"q1":null,"q1":null}', cases, choices, rows)


def test_consistency_quarantines_instability_without_using_gold_labels(monkeypatch):
    exp = probe(monkeypatch)
    cases = [{"id": "changed"}, {"id": "stable"}, {"id": "missing"}]
    calls = [
        {"reverse": False, "results": [{"case": "changed", "selected_index": 0},
                                        {"case": "stable", "selected_index": 1}]},
        {"reverse": True, "results": [{"case": "changed", "selected_index": 2},
                                       {"case": "stable", "selected_index": 1}]}]
    result = exp.consistency_report(calls, cases)
    assert [r["status"] for r in result] == ["quarantined_order_sensitive",
                                            "stable_but_unverified",
                                            "quarantined_missing_valid_response"]
    assert all(r["graph_ready"] is False for r in result)
