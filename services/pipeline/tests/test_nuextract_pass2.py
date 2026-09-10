import importlib
import json
from pathlib import Path


def test_native_payload_separates_template_instructions_and_document(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "experiments"))
    exp = importlib.import_module("nuextract_pass2")
    h = exp.harness
    rows = h.passages("小明受伤了。")
    schema = h.slot_schema(["c1"], rows, ["life_status"])
    prompt = h.compiler_prompt(rows, "CANDIDATE_1: injury") + "\n" + h.FIELD_GUIDANCE
    prompt += "\nRequired output schema:\n" + json.dumps(schema)
    payload = exp.native_payload({"messages": [{"role": "system", "content": "old"},
                                               {"role": "user", "content": prompt}], "format": schema})
    assert [m["role"] for m in payload["messages"]] == ["template", "instructions", "user"]
    template = json.loads(payload["messages"][0]["content"])
    assert template["c1"]["subject"] == "verbatim-string"
    assert template["c1"]["kind"] == ["life_status"]
    assert template["c1"]["evidence"][0]["passage_id"] == ["p1"]
    document = payload["messages"][-1]["content"]
    assert "小明受伤了。" in document and "CANDIDATE_1" in document
    assert "Write predicate" not in document and "Required output schema" not in document
    assert payload["format"] == schema
