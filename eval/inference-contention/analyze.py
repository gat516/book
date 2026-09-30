#!/usr/bin/env python3
"""Summarize metadata-only benchmark JSONL without making tail-latency claims."""

import argparse
import json
import math
import statistics
from pathlib import Path


def distribution(values):
    values = [float(x) for x in values if x is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values), 3),
            "min": round(min(values), 3), "max": round(max(values), 3)}


def correlation(pairs):
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    denominator = math.sqrt(sum((x - xm) ** 2 for x in xs) * sum((y - ym) ** 2 for y in ys))
    if not denominator:
        return None
    return round(sum((x - xm) * (y - ym) for x, y in pairs) / denominator, 6)


def summarize(events):
    starts = [x for x in events if x.get("event") == "start"]
    finishes = [x for x in events if x.get("event") == "finish"]
    samples = [dict(x) for x in events if x.get("event") == "sample"]
    usage = finishes[-1]["metadata"].get("usage_events", []) if finishes else []
    for sample in samples:
        matches = [x for x in usage if x[1] == "ask"
                   and sample["started"] <= float(x[0]) <= sample["finished"]]
        if len(matches) == 1:
            sample["input_tokens"], sample["output_tokens"] = matches[0][4:6]
        ticks = sample.get("observed_ticks", 0)
        # At least 80% of expected half-second observations, with one edge tick
        # of tolerance. Missing telemetry must not become evidence of idle time.
        sample["observation_valid"] = ticks >= max(1, sample["seconds"] * 1.6 - 1)
        sample["overlap_fraction"] = sample.get("active_ticks", 0) / ticks if ticks else None

    def success(sample):
        return sample.get("status") == 200 and bool(sample.get("served_by"))

    def valid(sample):
        return success(sample) and sample["observation_valid"]

    def stats(rows):
        good = [x for x in rows if success(x)]
        return {"attempts": len(rows), "successes": len(good),
                "http_429": sum(x.get("status") == 429 for x in rows),
                "provider_limit_errors": sum(x.get("category") in {"rate_limited", "quota_exhausted"} for x in rows),
                "latency_seconds": distribution([x["seconds"] for x in good]),
                "input_tokens": distribution([x.get("input_tokens") for x in good]),
                "output_tokens": distribution([x.get("output_tokens") for x in good])}

    baseline = [x for x in samples if x["phase"] == "baseline" and valid(x) and x["overlap_fraction"] == 0]
    recovery = [x for x in samples if x["phase"] == "recovery" and valid(x) and x["overlap_fraction"] == 0]
    full = [x for x in samples if x["phase"] == "during" and valid(x) and x["overlap_fraction"] >= .95]
    partial = [x for x in samples if x["phase"] == "during" and valid(x) and 0 < x["overlap_fraction"] < .95]
    known = [x for x in samples if success(x)]
    identities = {(x["served_by"].get("provider"), x["served_by"].get("model"), x.get("gate"), x.get("source_fingerprint")) for x in known}
    comparison = {"context_and_model_consistent": len(identities) == 1,
                  "full_overlap_samples": len(full), "idle_control_samples": len(baseline + recovery),
                  "evidence_grade": "single_chapter_screening_only"}
    if full and baseline and recovery and len(identities) == 1:
        busy = statistics.median(x["seconds"] for x in full)
        comparison.update({"busy_to_baseline_median_ratio": round(busy / statistics.median(x["seconds"] for x in baseline), 3),
                           "busy_to_recovery_median_ratio": round(busy / statistics.median(x["seconds"] for x in recovery), 3),
                           "busy_to_pooled_idle_median_ratio": round(busy / statistics.median(x["seconds"] for x in baseline + recovery), 3)})
    return {"complete": bool(starts and finishes), "all_requests": stats(samples),
            "reported_output_tokens_vs_latency_pearson_r": correlation([
                (float(x["output_tokens"]), x["seconds"]) for x in known if x.get("output_tokens") is not None]),
            "phases": {phase: stats([x for x in samples if x["phase"] == phase])
                       for phase in ("baseline", "during", "recovery")},
            "fully_overlapping": stats(full), "partially_overlapping": stats(partial),
            "comparison": comparison,
            "requests": [{k: v for k, v in x.items() if k != "event"} for x in samples],
            "final_metadata": finishes[-1]["metadata"] if finishes else None,
            "stage_transitions": finishes[-1].get("transitions", []) if finishes else []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize([json.loads(x) for x in args.jsonl.read_text().splitlines() if x.strip()]), indent=2))
