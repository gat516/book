import runpy
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("analyze.py")))
summarize = MODULE["summarize"]


def sample(phase, seconds=10, busy=0, ticks=20, **changes):
    return {"event": "sample", "phase": phase, "started": 100, "finished": 100 + seconds,
            "seconds": seconds, "status": 200, "served_by": {"provider": "test", "model": "model"},
            "gate": 5, "source_fingerprint": "same", "active_ticks": busy, "observed_ticks": ticks, **changes}


def test_partial_and_missing_observations_are_not_full_overlap():
    result = summarize([sample("during", busy=10), sample("during", busy=0, ticks=0),
                        sample("during", busy=20)])
    assert result["fully_overlapping"]["attempts"] == 1
    assert result["partially_overlapping"]["attempts"] == 1
    assert result["complete"] is False


def test_failures_and_no_model_responses_are_not_success_latency():
    result = summarize([sample("during", busy=20, status=429, category="rate_limited", served_by=None),
                        sample("baseline", seconds=0.1, served_by=None)])
    assert result["all_requests"]["provider_limit_errors"] == 1
    assert result["all_requests"]["latency_seconds"] == {"n": 0}


def test_comparison_requires_both_controls_and_same_context():
    requests = [sample("baseline"), sample("during", seconds=20, busy=40, ticks=40), sample("recovery")]
    assert summarize(requests)["comparison"]["busy_to_pooled_idle_median_ratio"] == 2
    requests[-1]["source_fingerprint"] = "different"
    assert "busy_to_pooled_idle_median_ratio" not in summarize(requests)["comparison"]


def test_usage_requires_unique_matching_success():
    finish = {"event": "finish", "metadata": {"usage_events": [[105, "ask", "test", "model", 1200, 150]]}}
    assert summarize([sample("baseline"), finish])["requests"][0]["output_tokens"] == 150
    finish["metadata"]["usage_events"].append([106, "ask", "test", "model", 1200, 180])
    assert "output_tokens" not in summarize([sample("baseline"), finish])["requests"][0]


def test_correlation_requires_sufficient_varying_samples():
    correlation = MODULE["correlation"]
    assert correlation([(1, 3), (2, 5), (3, 7)]) == 1
    assert correlation([(1, 3), (1, 5), (1, 7)]) is None
    assert correlation([(1, 3), (2, 5)]) is None
