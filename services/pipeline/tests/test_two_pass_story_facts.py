from pathlib import Path
import json
import pytest


def experiment_module():
    # experiments is intentionally not a production package. Importing by path keeps the
    # worker's module graph unaware of this read-only trial.
    import importlib.util
    import sys

    path = Path(__file__).parents[1] / "experiments/two_pass_story_facts.py"
    spec = importlib.util.spec_from_file_location("two_pass_story_facts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_evidence_is_materialized_only_from_the_cited_source():
    exp = experiment_module()
    source = "Alice distrusted Bob.\n\nBob left."
    rows = exp.passages(source)
    claims = exp.StoryClaims.model_validate(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "kind": "relationship_attitude",
                    "subject": "Alice",
                    "predicate": "distrusts",
                    "value": "Alice distrusts Bob",
                    "linked_entities": [{"surface": "Bob", "role": "target"}],
                    "epistemic_status": "explicit",
                    "temporal_status": "current",
                    "importance": "significant",
                    "evidence": [{"passage_id": "p1"}],
                    "rationale": "A durable relationship attitude.",
                }
            ]
        }
    )

    accepted, rejected = exp.validate_evidence(claims, rows)

    assert rejected == []
    evidence = accepted[0]["evidence"][0]
    assert source[evidence["char_start"] : evidence["char_end"]] == "Alice distrusted Bob."


def test_invented_subject_rejects_the_whole_claim():
    exp = experiment_module()
    rows = exp.passages("Alice looked at Bob.")
    claim = {
        "claim_id": "c1",
        "kind": "romantic_feeling",
        "subject": "Alice and Bob",
        "predicate": "loves",
        "value": "Alice loves Bob",
        "linked_entities": [{"surface": "Bob", "role": "target"}],
        "epistemic_status": "strongly_implied",
        "temporal_status": "current",
        "importance": "significant",
        "evidence": [{"passage_id": "p1"}],
        "rationale": "Supposed romantic cue.",
    }

    accepted, rejected = exp.validate_evidence(exp.StoryClaims(claims=[claim]), rows)

    assert accepted == []
    assert rejected[0]["rejection"] == "subject is not an exact source surface"


def test_saved_failure_is_detected_and_cards_are_not_collapsed():
    exp = experiment_module()
    path = Path(__file__).parents[3] / "eval/knowledge/ling-3.0-tiny-ch1-twopass.json"
    saved = json.loads(path.read_text())
    assert len(exp.candidate_cards(saved["analysis"])) == 15
    report = exp.quality_report(saved["claims"])
    assert len(report["constant_fields"]) == 4
    assert len(report["claim_issues"]) == 15
    assert report["needs_review"]
    assert not report["semantic_support_verified"]


def test_keyed_schema_is_inline_and_rejects_schema_vocabulary():
    exp = experiment_module()
    schema = exp.slot_schema(["c1", "c2"], exp.passages("Alice left."), ["event"])
    assert "$ref" not in json.dumps(schema)
    assert exp.materialize_slots('{"c1":null,"c2":null}', ["c1", "c2"], schema).claims == []
    with pytest.raises(ValueError):
        exp.materialize_slots('{"c1":null}', ["c1", "c2"], schema)
    with pytest.raises(ValueError, match="duplicate"):
        exp.materialize_slots('{"c1":null,"c1":null,"c2":null}', ["c1", "c2"], schema)
    item = dict(kind="subject", subject="Alice", predicate="location", value="away",
                linked_entities=[], epistemic_status="explicit", temporal_status="current",
                importance="significant", evidence=[{"passage_id": "p1"}], rationale="She left.")
    with pytest.raises(ValueError, match="kind"):
        exp.materialize_slots(json.dumps({"c1": item, "c2": None}), ["c1", "c2"], schema)


def test_translation_input_is_explicit_and_never_falls_back(tmp_path):
    exp = experiment_module()
    path = tmp_path / "chapter.json"
    path.write_text(json.dumps({"chapters": [{"chapter": 1, "source": "中文", "english": "English"}]}))
    assert exp.load_chapter(path, 1) == "中文"
    assert exp.load_chapter(path, 1, "english") == "English"
    with pytest.raises(KeyError):
        exp.load_chapter(path, 1, "translation")


@pytest.mark.parametrize("minimal", [False, True])
def test_replay_batches_every_candidate_and_exposes_schema(tmp_path, monkeypatch, minimal):
    import asyncio
    from types import SimpleNamespace
    exp = experiment_module()
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            self._options = {}

        async def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            schema = kwargs["json_schema"]
            return SimpleNamespace(text=json.dumps({ref: None for ref in schema["required"]}),
                                   served_model="test", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(exp, "OllamaProvider", Provider)
    root = Path(__file__).parents[3]
    args = exp.parser().parse_args(["--replay-analysis", str(root / "eval/knowledge/ling-3.0-tiny-ch1-twopass.json"),
                                   "--output", str(tmp_path / "result.json")] + (["--minimal"] if minimal else []))
    result = asyncio.run(exp.run(args))
    assert len(calls) == 5
    assert calls[-1][1]["json_schema"]["required"] == ["c13", "c14", "c15"]
    assert all("Required output schema:" in prompt for prompt, _ in calls)
    assert result["usage"]["analysis"]["replayed"]
    assert result["status"] == "completed"
    assert result["graph_ready"] is False


def test_language_diagnostic_allows_names_but_flags_chinese_prose():
    exp = experiment_module()
    claim = dict(claim_id="c1", kind="event", subject="小明", predicate="location",
                 value="小明 is outside", rationale="小明 left", linked_entities=[],
                 epistemic_status="explicit", temporal_status="current", importance="significant")
    assert exp.quality_report([claim])["cjk_prose_claim_counts"] == {}
    claim["value"] = "小明走到外面"
    assert exp.quality_report([claim])["cjk_prose_claim_counts"] == {"value": 1}


def test_minimal_output_contains_only_statement_and_source_evidence():
    exp = experiment_module()
    rows = exp.passages("小明受伤了。")
    schema = exp.minimal_schema(["c1", "c2"], rows)
    assert set(schema["properties"]["c1"]["anyOf"][0]["properties"]) == {"statement", "passage_ids"}
    raw = '{"c1":{"statement":"小明受伤了。","passage_ids":["p1"]},"c2":null}'
    result = exp.materialize_minimal(raw, ["c1", "c2"], rows)
    assert len(result) == 1
    assert result[0]["evidence"][0]["text"] == "小明受伤了。"
    assert result[0]["review_required"]
    with pytest.raises(ValueError, match="evidence"):
        exp.materialize_minimal(raw.replace('"p1"', '"p99"'), ["c1", "c2"], rows)
    body = json.loads(raw)
    body["c1"]["linked_entities"] = []
    with pytest.raises(ValueError, match="fields"):
        exp.materialize_minimal(json.dumps(body), ["c1", "c2"], rows)


def test_direct_minimal_uses_one_call_and_no_analyst_notes(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    exp = experiment_module()
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            self._options = {}

        async def complete(self, prompt, **kwargs):
            calls.append(prompt)
            return SimpleNamespace(text='{"statements":[]}',
                                   served_model="test", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(exp, "OllamaProvider", Provider)
    args = exp.parser().parse_args(["--minimal", "--source-only"])
    result = asyncio.run(exp.run(args))
    assert len(calls) == 1
    assert "untrusted_analyst_notes" not in calls[0]
    assert result["statements"] == []
    assert result["usage"]["calls"] == 1
    assert result["graph_ready"] is False
