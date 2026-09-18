import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from linked_memory import validate_linked_memory
from pipeline.fact_first import _source_passages


def fixture():
    case = {"source": "凌峰就是龍飛。"}
    pid = _source_passages(case["source"])[0]["id"]
    entity = {"id": "e1", "names": ["凌峰", "龍飛"], "passages": [pid]}
    note = {"note": case["source"], "passages": [pid], "entities": ["e1"], "unresolved": []}
    return case, {"entities": [entity], "notes": [note]}


def test_explicit_model_identity_and_citations_survive():
    case, data = fixture()
    out = validate_linked_memory(json.dumps(data), case)
    assert out["structurally_valid"]
    assert out["entities"][0]["names"] == ["凌峰", "龍飛"]
    assert out["semantic_review"] == "pending"


def test_same_spelling_never_merges_ids():
    case, data = fixture()
    data["entities"].append({**data["entities"][0], "id": "e2"})
    assert len(validate_linked_memory(json.dumps(data), case)["entities"]) == 2


def test_invented_alias_prevents_binding_but_retains_note():
    case, data = fixture()
    data["entities"][0]["names"].append("invented")
    out = validate_linked_memory(json.dumps(data), case)
    assert not out["structurally_valid"]
    assert out["notes"][0]["note"] == data["notes"][0]["note"]
    assert out["notes"][0]["entities"] == []


def test_unknown_id_rejected():
    case, data = fixture()
    data["notes"][0]["entities"] = ["unknown"]
    with pytest.raises(ValueError, match="undeclared"):
        validate_linked_memory(json.dumps(data), case)
