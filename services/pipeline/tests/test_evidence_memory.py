"""Evidence experiment: identity authority, spoiler gates, immutable jobs and resume costs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from evidence_memory import (
    digest, earlier_entities, enrichment_payload, estimate_tokens, extraction_request, gated_records, passages_for, policy_for,
    resolution_payload, retrieve, search_source, validate_enrichment, validate_resolution, validate_selection,
    wire,
)
from evidence_jobs import EvidenceJobs, JobStopped, freeze_json, write_json
from evidence_trial import enrich, extract, load_artifact
from pipeline.llm.provider import AdmissionRejected, Completion


SOURCE = "凌峰就是龍飛。\n\n凌峰承諾明天幫助安若素。\n\n安若素接受了。\n"


def case(chapter=1):
    return {"chapter": chapter, "source": SOURCE, "ontology": {"kinds": ["person", "group"]}}


def reply():
    return {"entities": [
        {"id": "e1", "kind": "person", "names": ["凌峰", "龍飛"], "passages": [1], "same_as": "new"},
        {"id": "e2", "kind": "person", "names": ["安若素"], "passages": [2], "same_as": None}],
        "records": [{"id": "r1", "topic": "commitment", "passages": [2, 3],
                     "entities": ["e1", "e2"], "unresolved": []}]}


def validate(data=None, candidates=(), source_case=None):
    return validate_selection(json.dumps(data or reply()), source_case or case(), policy_for(), candidates, "fixture")


def artifact():
    return {"format": "evidence-memory-v1", "novel_id": "book1", "generation": "g1",
            "source_chapter": 1, "case": case(), "result": validate(), "candidates": [],
            "policy": policy_for(), "artifact_hash": "fixture"}


def args(tmp_path, **changes):
    return SimpleNamespace(**(dict(output=tmp_path, case=tmp_path / "case.json", model="test-model",
                                  reasoning="none", output_tokens=900, prior=[], policy=None,
                                  at=1, records=["r1"], command="enrich") | changes))


class Fake:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def complete(self, **request):
        self.requests.append(request)
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return Completion(text=response if isinstance(response, str) else json.dumps(response),
                          served_provider="fake", served_model="actual-model",
                          input_tokens=100, output_tokens=50)

    async def aclose(self):
        pass


def test_source_attached_exactly_and_unresolved_does_not_drop_evidence():
    out = validate()
    record = out["records"][0]
    assert record["source_chapter"] == 1
    assert record["unresolved"] == ["安若素"]
    assert len(record["entities"]) == 1
    for passage in record["evidence"]:
        assert SOURCE[passage["char_start"]:passage["char_end"]] == passage["text"]
    assert all("entity_id" not in o for o in out["occurrence_candidates"])
    assert len([o for o in out["occurrence_candidates"] if o["surface"] == "凌峰"]) == 2
    assert not out["published"] and out["semantic_review"] == "pending"


def test_model_must_explicitly_choose_existing_identity():
    candidates = [{"id": "prior1", "names": ["凌峰"], "kind": "person"}]
    data = reply()
    out = validate(data, candidates)
    assert out["entities"][0]["identity_id"] != "prior1"
    data["entities"][0]["same_as"] = "prior1"
    assert validate(data, candidates)["entities"][0]["identity_id"] == "prior1"
    data["entities"][0]["same_as"] = "unsupplied"
    assert "凌峰" in validate(data, candidates)["records"][0]["unresolved"]


def test_local_occurrences_do_not_merge_same_spelling():
    data = reply()
    data["entities"][1] = {**data["entities"][0], "id": "e2"}
    out = validate(data)
    assert len({e["identity_id"] for e in out["entities"]}) == 2
    assert len([o for o in out["occurrence_candidates"] if o["surface"] == "凌峰"]) == 2


def test_only_unambiguous_existing_ordinals_are_recovered():
    data = reply()
    data["entities"][0]["passages"] = ["p001"]
    data["records"][0]["passages"] = ["p2", "003", 2]
    out = validate(data)
    assert out["records"][0]["passages"] == [2, 3]
    assert any(d["action"] == "normalize ordinal" for d in out["diagnostics"])
    data["records"][0]["passages"] = [999]
    assert validate(data)["records"] == []
    data["records"][0]["passages"] = [True]
    assert validate(data)["records"] == []


def test_bad_alias_does_not_invalidate_witnessed_primary_identity():
    data = reply()
    data["entities"][0]["names"].append("invented")
    out = validate(data)
    assert out["entities"][0]["names"] == ["凌峰", "龍飛"]
    assert out["records"][0]["entities"]
    assert "invented" in out["records"][0]["unresolved"]


def test_misplaced_local_id_recovers_surface_without_binding_identity():
    data = reply()
    data["records"][0].update(entities=[], unresolved=["e1"])
    out = validate(data)
    record = out["records"][0]
    assert record["unresolved"] == ["凌峰", "龍飛"]
    assert record["entities"] == record["local_entities"] == []
    assert out["entities"] == []
    assert record["passages"] == [2, 3]
    assert out["diagnostics"][0]["action"] == "expand unresolved local ID to source names"


def test_invalid_identity_cannot_supply_invented_unresolved_names():
    data = reply()
    data["entities"][0]["names"] = ["fabricated name"]
    data["records"][0].update(entities=[], unresolved=["e1"])
    record = validate(data)["records"][0]
    assert record["unresolved"] == ["unknown local ID: e1"]
    assert not record["entities"]


def test_reading_windows_cover_entire_source_once_in_one_request():
    c = case()
    c["source"] = "\n\n".join(f"Passage {i}." for i in range(53))
    request = extraction_request(c, policy_for(), [], model="test", reasoning="none", output_tokens=3000)
    payload = json.loads(request["prompt"])
    ids = [pid for start, end in payload["reading_sections"] for pid in range(start, end + 1)]
    assert ids == [pid for pid, _ in payload["passages"]]
    assert len(ids) == 53 and len(set(ids)) == 53
    assert request["max_output_tokens"] == 3000


def test_unused_entities_and_unrequested_topics_are_not_retained():
    data = reply()
    data["entities"].append({**data["entities"][0], "id": "unused"})
    data["records"].append({**data["records"][0], "id": "r2", "topic": "incidental pose"})
    out = validate(data)
    assert len(out["records"]) == 1 and len(out["entities"]) == 2


def test_policy_is_data_and_accepts_custom_priorities():
    policy = policy_for({"priorities": {"clue": "Evidence useful to solve the mystery"}})
    data = reply()
    data["records"][0]["topic"] = "clue"
    assert validate_selection(json.dumps(data), case(), policy, [], "x")["records"][0]["topic"] == "clue"
    with pytest.raises(ValueError):
        policy_for({"candidate_limit": False})


@pytest.mark.parametrize("change", [{"source_chapter": 2}, {"source_chapter": 5},
                                   {"novel_id": "other"}, {"generation": "other"}])
def test_earlier_candidates_reject_future_or_mixed_inputs(change):
    prior = artifact() | change
    with pytest.raises(ValueError, match="earlier chapter"):
        earlier_entities([prior], novel_id="book1", chapter=2, generation="g1")


def test_candidate_retrieval_never_binds_names_and_respects_limit():
    previous = earlier_entities([artifact()], novel_id="book1", chapter=2, generation="g1")
    original = deepcopy(previous)
    candidates = retrieve("龍飛", previous, 1)
    assert candidates and previous == original
    assert "same_as" not in candidates[0]


def test_enrichment_gate_uses_knowledge_chapter():
    with pytest.raises(ValueError, match="knowledge chapter"):
        gated_records(artifact(), ["r1"], 0)
    assert gated_records(artifact(), ["r1"], 1)
    with pytest.raises(ValueError):
        gated_records(artifact(), ["invented"], 1)


def test_search_retains_details_not_selected_into_evidence():
    a = artifact()
    a["result"]["records"] = []
    hits = search_source(a, "龍飛", 1)
    assert hits[0]["text"] == "凌峰就是龍飛。"
    with pytest.raises(ValueError, match="knowledge chapter"):
        search_source(a, "龍飛", 0)


def test_enrichment_uses_readable_source_and_drops_unrelated_names():
    a = artifact()
    payload = enrichment_payload(a["result"]["records"], a, {"凌峰": "Ling Feng", "future": "Spoiler"})
    assert payload["naming_map"] == {"凌峰": "Ling Feng"}
    assert "凌峰" in payload["passages"]["2"]
    assert "⟦" not in json.dumps(payload)
    good = {"notes": {"r1": "Ling Feng promises to help An Ruosu tomorrow."}, "names": {"安若素": "An Ruosu"}}
    assert validate_enrichment(json.dumps(good), payload)["status"] == "draft"
    good["names"] = {"凌峰": "Invented spelling"}
    with pytest.raises(ValueError, match="spelling"):
        validate_enrichment(json.dumps(good), payload)


def test_context_is_bounded_deduplicated_and_does_not_mutate_evidence():
    a = artifact()
    records = a["result"]["records"]
    records.append({**deepcopy(records[0]), "id": "r2"})
    original = deepcopy(a)
    base = enrichment_payload(records, a, {}, include_context=False)
    enriched = enrichment_payload(records, a, {})
    assert enriched["passages"]["1"] == "凌峰就是龍飛。"
    assert all(r["context"] == [1] for r in enriched["records"])
    assert len(enriched["passages"]) == 3
    assert estimate_tokens(wire(enriched)) <= estimate_tokens(wire(base)) + 400
    assert a == original
    assert enrichment_payload(records, a, {}, input_token_budget=estimate_tokens(wire(base))) == base
    a["policy"]["max_context_tokens"] = 0
    assert enrichment_payload(records, a, {}) == base


def test_context_fills_short_gap_without_adding_it_to_record_evidence():
    a = artifact()
    a["case"]["source"] = "A gained a power.\n\nIt only works at night.\n\nA might defeat B."
    p = passages_for(a["case"]["source"])
    a["result"]["records"] = [{"id": "r1", "topic": "ability", "passages": [1, 3],
                                "evidence": [p[1], p[3]], "local_entities": [], "unresolved": []}]
    payload = enrichment_payload(a["result"]["records"], a, {})
    assert payload["records"][0]["passages"] == [1, 3]
    assert payload["records"][0]["context"] == [2]
    assert payload["passages"]["2"] == "It only works at night."


def test_unresolved_surface_can_receive_spelling_without_identity_binding():
    a = artifact()
    a["result"]["entities"] = []
    record = a["result"]["records"][0]
    record.update(local_entities=[], entities=[], unresolved=["龍飛", "unknown local ID: e9"])
    original = deepcopy(a)
    payload = enrichment_payload([record], a, {})
    assert payload["source_names"] == ["龍飛"]
    result = validate_enrichment(json.dumps({"notes": {"r1": "A promise was made."},
                                            "names": {"龍飛": "Long Fei"}}), payload)
    assert result["names"] == {"龍飛": "Long Fei"}
    assert a == original and record["entities"] == []


def test_context_budget_policy_rejects_invalid_values():
    for value in (-1, True, "400"):
        with pytest.raises(ValueError, match="context budget"):
            policy_for({"max_context_tokens": value})
    assert policy_for({"max_context_tokens": 0})["max_context_tokens"] == 0


def test_resolution_is_selected_and_does_not_rewrite_records():
    a = artifact()
    a["candidates"] = [{"id": "older", "kind": "person", "names": ["安若素"]}]
    original = deepcopy(a)
    payload = resolution_payload(a["result"]["records"], a, 10)
    assert payload["references"] == [{"record": "r1", "reference": "安若素"}]
    response = {"decisions": [{"record": "r1", "reference": "安若素", "candidate": "older", "passages": [2]}]}
    assert validate_resolution(json.dumps(response), payload)["decisions"][0]["candidate"] == "older"
    assert a == original
    response["decisions"][0]["candidate"] = "missing"
    with pytest.raises(ValueError, match="unknown identity"):
        validate_resolution(json.dumps(response), payload)


async def test_budget_and_failed_usage_survive_restarts(tmp_path):
    fake = Fake([AdmissionRejected("wait", category="rate_limited"), {"ok": True}])
    request = {"prompt": "source", "model": "requested"}
    with EvidenceJobs(tmp_path, {"provider": "fake"}, max_attempts=1, interval=0) as jobs:
        with pytest.raises(AdmissionRejected):
            await jobs.call("extract", request, {}, lambda: fake)
    with EvidenceJobs(tmp_path, {"provider": "fake"}, max_attempts=1, interval=0, retry_failed=True) as jobs:
        with pytest.raises(JobStopped, match="budget"):
            await jobs.call("extract", request, {}, lambda: fake)
        assert jobs.report()["unknown_usage_attempts"] == 1
    with EvidenceJobs(tmp_path, {"provider": "fake"}, max_attempts=2, interval=0, retry_failed=True) as jobs:
        completion, key = await jobs.call("extract", request, {}, lambda: fake)
        assert completion["served_model"] == "actual-model"
        assert jobs.report()["new_successful_usage"]["output_tokens"] == 50
    with EvidenceJobs(tmp_path, {"provider": "fake"}, max_attempts=0, replay_only=True) as jobs:
        assert (await jobs.call("extract", request, {}, lambda: fake))[0] == completion
        assert jobs.report()["cumulative_successful_usage"]["input_tokens"] == 100
        assert jobs.report()["unknown_usage_attempts"] == 1
        assert jobs.report()["new_attempts"] == 0
    assert len(fake.requests) == 2


async def test_killed_attempt_is_counted_and_never_automatically_retried(tmp_path):
    with EvidenceJobs(tmp_path, {}, max_attempts=2, interval=0) as jobs:
        key = jobs.key({"prompt": "x"}, {})
        write_json(tmp_path / "attempts/00001.json", {"number": 1, "key": key, "started_at": 0, "status": "started", "usage": None})
        with pytest.raises(JobStopped, match="interrupted"):
            await jobs.call("extract", {"prompt": "x"}, {}, lambda: pytest.fail("must not call"))
        assert jobs.report()["unknown_usage_attempts"] == 1


async def test_cooldown_blocks_different_job_without_spending(tmp_path):
    fake = Fake([AdmissionRejected("wait", category="rate_limited", retry_after_s=60)])
    with EvidenceJobs(tmp_path, {}, max_attempts=2, interval=0) as jobs:
        with pytest.raises(AdmissionRejected):
            await jobs.call("extract", {"prompt": "x"}, {}, lambda: fake)
        with pytest.raises(JobStopped, match="cooldown"):
            await jobs.call("enrich", {"prompt": "y"}, {}, lambda: fake)
        assert jobs.report()["attempts_total"] == 1


def test_immutable_artifacts_and_single_writer(tmp_path):
    freeze_json(tmp_path / "artifact.json", {"x": 1})
    with pytest.raises(ValueError, match="immutable"):
        freeze_json(tmp_path / "artifact.json", {"x": 2})
    with EvidenceJobs(tmp_path, {}) as first:
        with pytest.raises(JobStopped, match="another evidence"):
            with EvidenceJobs(tmp_path, {}):
                pass
        assert first.report()["attempts_total"] == 0


async def test_end_to_end_extract_optional_enrich_and_free_resume(tmp_path):
    identity = {"provider": "fake"}
    saved = {"novel_id": "book1", "case": case()}
    write_json(tmp_path / "case.json", saved)
    fake = Fake([reply(), {"notes": {"r1": "Ling Feng promises to help An Ruosu tomorrow."},
                          "names": {"安若素": "An Ruosu"}}])
    with EvidenceJobs(tmp_path, identity, max_attempts=1, interval=0) as jobs:
        result = await extract(args(tmp_path), jobs, lambda: fake, identity)
        assert result["status"] == "evidence_ready_for_review"
        assert len(fake.requests) == 1  # no automatic naming, resolution or rendering
    evidence_bytes = (tmp_path / "evidence.json").read_bytes()
    a = load_artifact(tmp_path / "evidence.json")
    with EvidenceJobs(tmp_path, identity, max_attempts=2, interval=0) as jobs:
        out = await enrich(args(tmp_path), jobs, lambda: fake, a, {"凌峰": "Ling Feng"})
        assert out["status"] == "drafts_ready_for_review"
    with EvidenceJobs(tmp_path, identity, max_attempts=0, replay_only=True) as jobs:
        out = await enrich(args(tmp_path), jobs, lambda: pytest.fail("replay must be free"), a, {"凌峰": "Ling Feng"})
        assert out["status"] == "drafts_ready_for_review"
        assert jobs.report()["new_attempts"] == 0
    assert (tmp_path / "evidence.json").read_bytes() == evidence_bytes
    assert "Draft English note" in (tmp_path / "enrichment-preview.md").read_text()
    assert len(fake.requests) == 2


async def test_failed_enrichment_preserves_evidence_and_paid_response(tmp_path):
    write_json(tmp_path / "case.json", {"novel_id": "book1", "case": case()})
    fake = Fake([reply(), {"notes": {"r1": "text"}, "names": {"future": "unknown"}}])
    with EvidenceJobs(tmp_path, {}, max_attempts=2, interval=0) as jobs:
        await extract(args(tmp_path), jobs, lambda: fake, {})
        a = load_artifact(tmp_path / "evidence.json")
        with pytest.raises(ValueError, match="invalid enrichment"):
            await enrich(args(tmp_path), jobs, lambda: fake, a, {})
        with pytest.raises(ValueError, match="invalid enrichment"):
            await enrich(args(tmp_path), jobs, lambda: fake, a, {})
        assert load_artifact(tmp_path / "evidence.json") == a
        assert jobs.report()["attempts_total"] == 2
    assert len(fake.requests) == 2


async def test_optional_context_does_not_split_an_existing_batch(tmp_path):
    a = artifact()
    records = a["result"]["records"]
    records.append({**deepcopy(records[0]), "id": "r2"})
    system = (Path(__file__).resolve().parents[1] / "experiments/prompts/evidence-enrich-v1.txt").read_text()
    base = enrichment_payload(records, a, {}, include_context=False)
    a["policy"]["max_enrichment_input_tokens"] = estimate_tokens(system + wire(base))
    fake = Fake([{"notes": {"r1": "A promise.", "r2": "Another promise."}, "names": {}}])
    with EvidenceJobs(tmp_path, {}, max_attempts=1, interval=0) as jobs:
        result = await enrich(args(tmp_path, records=["r1", "r2"]), jobs, lambda: fake, a, {})
    assert result["jobs"] == len(fake.requests) == 1
    sent = json.loads(fake.requests[0]["prompt"])
    assert all(r["context"] == [] for r in sent["records"])
    assert sent["passages"] == base["passages"]
