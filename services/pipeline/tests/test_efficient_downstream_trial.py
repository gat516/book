"""Offline guards for the local efficiency experiment; no provider/DB required."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from downstream_trial import LocalCalls
from efficient_downstream_trial import (
    accept_rendering, apply_delta, choose_identity_batch, choose_render_batch,
    evidence_for, link_notes, mark_terms, retrieve_candidates, usage_report, validate_delta,
)
from pipeline.llm.provider import AdmissionRejected, Completion


def entity(eid="b1e1", name="凌峰"):
    return {"id": eid, "kind": "character", "names": [name], "passages": ["p1"], "candidate": None}


def args(tmp_path, **changes):
    values = dict(output=tmp_path, input_budget=2400, candidate_limit=3, link_output_tokens=900,
                  render_output_tokens=900, identity_followups=1, model="test")
    return SimpleNamespace(**(values | changes))


def test_existing_identity_needs_only_explicit_id():
    registry = {"b1e1": entity()}
    raw = {"new": [], "aliases": [], "links": [["n2", ["b1e1"], []]]}
    checked, aliases = validate_delta(raw, {"n2": {}}, {}, ["character"], registry, "b2e")
    apply_delta(registry, checked, aliases)
    assert checked["links"][0]["entities"] == ["b1e1"]
    assert registry["b1e1"]["names"] == ["凌峰"]


def test_equivalent_name_array_still_requires_source_witness():
    raw = {"new": [["b1e1", "character", ["凌峰", "invented"], ["p1"]]],
           "aliases": [], "links": [["n1", ["b1e1"], []]]}
    checked, _ = validate_delta(raw, {"n1": {}}, {"p1": "凌峰来了"}, ["character"], {}, "b1e")
    assert checked["entities"][0]["names"] == ["凌峰"]


def test_new_spelling_match_does_not_merge_identity():
    registry = {"b1e1": entity()}
    raw = {"new": [["b2e1", "character", "凌峰", [], ["p2"]]], "aliases": [],
           "links": [["n2", ["b2e1"], []]]}
    checked, aliases = validate_delta(raw, {"n2": {}}, {"p2": "凌峰出现"},
                                      ["character"], registry, "b2e")
    apply_delta(registry, checked, aliases)
    assert set(registry) == {"b1e1", "b2e1"}


def test_unsupplied_candidate_cannot_be_linked():
    raw = {"new": [], "aliases": [], "links": [["n1", ["hidden_id"], []]]}
    with pytest.raises(ValueError, match="unknown entity link"):
        validate_delta(raw, {"n1": {}}, {}, ["character"], {}, "b1e")


def test_duplicate_id_is_normalized_without_merging_distinct_ids():
    registry = {"e1": entity("e1"), "e2": entity("e2")}
    raw = {"new": [], "aliases": [], "links": [["n1", ["e1", "e2", "e1"], []]]}
    checked, _ = validate_delta(raw, {"n1": {}}, {}, ["character"], registry, "b1e")
    assert checked["links"][0]["entities"] == ["e1", "e2"]
    assert checked["diagnostics"][0]["action"] == "deduplicate identical linked IDs"


def test_malformed_unresolved_references_remain_visible_for_review():
    raw = {"new": [], "aliases": [], "links": [["n1", [], ["p1", "invented", "凌峰"]]]}
    checked, _ = validate_delta(raw, {"n1": {}}, {"p1": "凌峰来了"}, ["character"], {}, "b1e")
    assert checked["links"][0]["unresolved"] == ["p1", "invented", "凌峰"]
    assert checked["diagnostics"][0]["invalid_references"] == ["p1", "invented"]


def test_single_unresolved_string_is_preserved_as_one_reference():
    raw = {"new": [], "aliases": [], "links": [["n1", [], "凌峰"]]}
    checked, _ = validate_delta(raw, {"n1": {}}, {"p1": "凌峰来了"}, ["character"], {}, "b1e")
    assert checked["links"][0]["unresolved"] == ["凌峰"]


def test_alias_delta_retains_primary_name_and_prior_evidence():
    registry = {"b1e1": entity()}
    raw = {"new": [], "aliases": [["b1e1", ["龍飛", "invented"], ["p2"]]],
           "links": [["n2", ["b1e1"], []]]}
    checked, aliases = validate_delta(raw, {"n2": {}}, {"p2": "凌峰也叫龍飛"},
                                      ["character"], registry, "b2e")
    apply_delta(registry, checked, aliases)
    assert registry["b1e1"]["names"] == ["凌峰", "龍飛"]
    assert registry["b1e1"]["passages"] == ["p1", "p2"]
    assert checked["diagnostics"]


def test_bad_witness_leaves_note_unresolved_without_discarding_it():
    raw = {"new": [["b1e1", "character", "invented", [], ["p1"]]], "aliases": [],
           "links": [["n1", ["b1e1"], []]]}
    checked, _ = validate_delta(raw, {"n1": {}}, {"p1": "凌峰"}, ["character"], {}, "b1e")
    assert checked["entities"] == []
    assert checked["links"] == [{"note": "n1", "entities": [], "unresolved": ["invented"]}]


def test_citations_preserved_and_context_expansion_is_bounded():
    passages = {f"p{i}": f"“line {i}" for i in range(8)}
    notes = {"n1": {"note": "test", "passages": ["p3", "p5"]}}
    assert set(evidence_for(notes, passages)) == {"p2", "p3", "p4", "p5"}
    assert set(evidence_for(notes, passages, neighbors=True)) == {"p2", "p3", "p4", "p5", "p6"}
    with pytest.raises(ValueError, match="unknown source"):
        evidence_for({"n1": {"passages": ["future"]}}, passages)


def test_retrieval_limits_candidates_without_changing_registry():
    registry = {f"e{i}": entity(f"e{i}", f"名字{i}") for i in range(20)}
    registry["relevant"] = entity("relevant", "凌峰")
    result = retrieve_candidates({"n1": {"note": "凌峰来访"}}, {}, registry, 3)
    assert result[0]["id"] == "relevant"
    assert len(result) == 3 and len(registry) == 21


def test_batch_size_depends_on_work_and_keeps_every_note(tmp_path):
    notes = [(f"n{i}", {"note": "short", "passages": ["p1"]}) for i in range(8)]
    batch, _ = choose_identity_batch(notes, {"p1": "source"}, {"kinds": []}, {}, "b1e",
                                     args(tmp_path), "system")
    assert len(batch) > 2
    expensive = [("n0", {"note": "文" * 400, "passages": ["p1"]}), *notes[1:]]
    small, _ = choose_identity_batch(expensive, {"p1": "source"}, {"kinds": []}, {}, "b1e",
                                     args(tmp_path), "system")
    assert list(small) == ["n0"]
    with pytest.raises(ValueError, match="evidence retained"):
        choose_identity_batch(notes, {"p1": "文" * 3000}, {"kinds": []}, {}, "b1e",
                              args(tmp_path), "system")


def test_rendering_uses_shared_local_pinyin_and_glossary_spellings():
    notes = {"n1": {"note": "凌峰来到青云宗。"}, "n2": {"note": "凌峰离开青云宗。"}}
    marked, terms, spellings = mark_terms(notes, {"entities": [entity()]}, {"青云宗": "Azure Cloud Sect"})
    tid = next(t for t, source in terms.items() if source == "凌峰")
    sect = next(t for t, source in terms.items() if source == "青云宗")
    payload = {"notes": {"n1": marked["n1"]}, "new_terms": {tid: "凌峰"}}
    out = accept_rendering({"notes": {"n1": f"⟦{tid}⟧ arrived at ⟦{sect}⟧."},
                            "terms": {tid: ["Ling Feng", "chinese_person"]}}, payload, terms, spellings)
    assert out["n1"] == "Ling Feng arrived at Azure Cloud Sect."
    payload = {"notes": {"n2": marked["n2"]}, "new_terms": {}}
    out = accept_rendering({"notes": {"n2": f"⟦{tid}⟧ left ⟦{sect}⟧."}, "terms": {}}, payload, terms, spellings)
    assert out["n2"] == "Ling Feng left Azure Cloud Sect."


def test_marker_omission_does_not_commit_naming_map():
    spellings = {}
    with pytest.raises(ValueError, match="markers"):
        accept_rendering({"notes": {"n1": "He left."}, "terms": {"t1": ["Peak", "chinese_person"]}},
                         {"notes": {"n1": "⟦t1⟧离开"}, "new_terms": {"t1": "凌峰"}},
                         {"t1": "凌峰"}, spellings)
    assert spellings == {}


def test_render_batch_budget_includes_new_terms(tmp_path):
    items = [(f"n{i}", "⟦t1⟧走了。") for i in range(1, 7)]
    batch, payload = choose_render_batch(items, {"t1": "凌峰"}, {}, args(tmp_path), "system")
    assert len(batch) > 3 and payload["new_terms"] == {"t1": "凌峰"}


class FakeProvider:
    def __init__(self, responses):
        self.responses, self.count = iter(responses), 0

    async def complete(self, **kwargs):
        self.count += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return Completion(text=json.dumps(response), served_provider="fake", served_model="test",
                          input_tokens=100, output_tokens=50)


async def test_resume_reuses_checkpoint_and_reports_new_spend_separately(tmp_path):
    provider = FakeProvider([{"ok": True}, AdmissionRejected("wait", category="rate_limited")])
    calls = LocalCalls(provider, tmp_path, max_new_calls=2)
    request = {"prompt": "test", "model": "test"}
    await calls.call("a", request)
    await calls.call("a", request)
    with pytest.raises(AdmissionRejected):
        await calls.call("b", request)
    report = usage_report(calls, {"completion": {"input_tokens": 20, "output_tokens": 10}})
    assert report["new_successful_usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert report["reused_successful_usage"] == report["new_successful_usage"]
    assert report["unknown_usage_attempts"] == 1
    with pytest.raises(ValueError, match="budget exhausted"):
        await calls.call("c", request)
    await calls.call("a", request)  # budget does not block a free replay
    assert provider.count == 2


async def test_identity_checkpoint_replay_and_unresolved_followup(tmp_path):
    notes = {"n1": {"note": "凌峰来了", "passages": ["p1"]}}
    provider = FakeProvider([
        {"new": [], "aliases": [], "links": [["n1", [], ["凌峰"]]]},
        {"new": [["r1e1", "character", "凌峰", [], ["p1"]]], "aliases": [],
         "links": [["n1", ["r1e1"], []]]},
    ])
    calls = LocalCalls(provider, tmp_path)
    linked = await link_notes(args(tmp_path), calls, notes, {"p1": "凌峰来了"}, {"kinds": ["character"]}, {})
    assert linked["links"][0]["entities"] == ["r1e1"]
    assert not linked["links"][0]["unresolved"]
    replay = LocalCalls(provider, tmp_path)
    assert await link_notes(args(tmp_path), replay, notes, {"p1": "凌峰来了"}, {"kinds": ["character"]}, {}) == linked
    assert provider.count == 2 and all(a["reused"] for a in replay.attempts)
