import json

import pytest

from reasoning_pilot import (QUESTIONS, SCORE_KEYS, analyze, bootstrap_ratio, citations,
                             digest, guard_state, schedule, validate_judge)


def scores(value=4):
    return {**dict.fromkeys(SCORE_KEYS, value), "unsupported_claims": 0}


def successful_records():
    rows = []
    for item in schedule():
        for arm, elapsed, tokens in (("current", 10.0, 1000), ("low", 5.0, 500)):
            rows.append({"event": "answer", "pair": item["pair"], "question_id": item["question_id"],
                         "arm": arm, "success": True, "seconds": elapsed, "input_tokens": 123,
                         "output_tokens": tokens, "prompt_hash": str(item["pair"]),
                         "citations": {"invalid": 0, "malformed": 0}})
        rows.append({"event": "judgment", "pair": item["pair"],
                     "scores": {"current": scores(), "low": scores()}})
    return rows + [{"event": "finished"}]


def test_fixed_balanced_plan_and_call_budget():
    plan = schedule()
    assert plan == schedule()
    assert len(plan) == len(QUESTIONS) == 10
    assert len({p["question_index"] for p in plan}) == 10
    assert sum(p["order"][0] == "current" for p in plan) == 5
    assert sum(p["judge_a"] == "current" for p in plan) == 5
    assert all(set(p["order"]) == {"current", "low"} for p in plan)
    assert sum(len(p["order"]) + 1 for p in plan) == 30
    assert digest(plan) == digest(schedule())


def test_citations_reject_unknown_future_and_malformed():
    result = citations("[chunk:7 ch:170] [chunk:7 ch:171] [chunk:9 ch:4] [chunk:oops]",
                       [{"kind": "chunk", "id": 7, "chapter": 170}])
    assert result == {"labels": 3, "invalid": 2, "future": 1, "malformed": 1}
    assert citations("Context does not say.", []) == {"labels": 0, "invalid": 0, "future": 0, "malformed": 0}


@pytest.mark.parametrize("bad", [None, {}, {"A": scores()},
    {"A": {**scores(), "groundedness": True}, "B": scores()},
    {"A": {**scores(), "citations": 5}, "B": scores()},
    {"A": {**scores(), "explanation": "private"}, "B": scores()}])
def test_judge_schema_rejects_unexpected_data(bad):
    with pytest.raises(ValueError):
        validate_judge(bad)


def test_judge_schema_accepts_numbers_only():
    assert validate_judge(json.loads(json.dumps({"A": scores(), "B": scores(3)})))["B"]["groundedness"] == 3


def test_paired_known_effect_and_complete_screen():
    summary = analyze(successful_records())
    assert summary["complete"] and summary["promising_candidate"]
    assert summary["paired_latency"]["geometric_mean"] == pytest.approx(.5)
    assert summary["paired_latency"]["bootstrap_95_percent"] == pytest.approx([.5, .5])
    assert summary["arms"]["current"]["output_tokens"] == 10000
    assert summary["arms"]["low"]["output_tokens"] == 5000


def test_failed_call_stays_in_denominator_and_usage_is_unknown():
    rows = [{"event": "completion_attempt", "pair": 0, "kind": "answer", "arm": "current"},
            {"event": "call_failure", "pair": 0, "kind": "answer", "arm": "current", "raw_usage": {}}]
    result = analyze(rows)
    assert result["arms"]["current"]["attempts"] == 1
    assert result["arms"]["current"]["errors"] == 1
    assert result["arms"]["current"]["unknown_usage_calls"] == 1
    assert not result["complete"] and not result["promising_candidate"]


def test_truncated_response_counts_available_tokens():
    rows = [{"event": "completion_attempt", "pair": 0, "kind": "answer", "arm": "low"},
            {"event": "call_failure", "pair": 0, "kind": "answer", "arm": "low",
             "raw_usage": {"prompt_tokens": 100, "total_tokens": 8292, "completion_tokens": 8192}}]
    assert analyze(rows)["arms"]["low"]["output_tokens"] == 8192


def test_missing_finish_and_quality_loss_cannot_pass():
    rows = successful_records()
    assert not analyze(rows[:-1])["promising_candidate"]
    for row in rows:
        if row["event"] == "judgment":
            row["scores"]["low"] = scores(2)
    assert not analyze(rows)["promising_candidate"]


def test_mismatched_input_or_prompt_rejected():
    rows = successful_records()
    rows[0]["input_tokens"] += 1
    with pytest.raises(ValueError, match="unmatched"):
        analyze(rows)


def test_queue_and_reader_guardrails():
    state = {"progress": 170, "pending": 0, "processing": 0, "chapters": [], "usage": [], "failures": []}
    guard_state(state, state)
    for change in ({"progress": 169}, {"pending": 1}, {"processing": 1}, {"chapters": [1]},
                   {"usage": [1]}, {"failures": [1]}):
        with pytest.raises(RuntimeError):
            guard_state({**state, **change}, state)


def test_bootstrap_is_reproducible():
    assert bootstrap_ratio([.4, .8, 1.2]) == bootstrap_ratio([.4, .8, 1.2])


def test_unmatched_failure_not_a_fake_token_saving():
    rows = [r for r in successful_records() if r.get("pair", 0) == 0]
    rows += [{"event": "completion_attempt", "pair": 1, "kind": "answer", "arm": "current"},
             {"event": "call_failure", "pair": 1, "kind": "answer", "arm": "current",
              "raw_usage": {"completion_tokens": 8192, "prompt_tokens": 100}}]
    result = analyze(rows)
    assert result["arms"]["current"]["output_tokens"] == 9192
    assert result["matched_answer_totals"]["current"]["output_tokens"] == 1000
    assert result["matched_answer_totals"]["low"]["output_tokens"] == 500
    assert not result["promising_candidate"]


def test_resumed_study_discloses_interruption():
    rows = successful_records() + [{"event": "started"}, {"event": "started"}]
    result = analyze(rows)
    assert result["resumed"]
    assert "interrupted" in result["limitations"][0]
