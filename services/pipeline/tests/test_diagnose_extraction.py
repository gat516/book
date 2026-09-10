import asyncio
import json

from novel_llm import AdmissionRejected
from pipeline.diagnose_extraction import run_diagnostic
from novel_llm.provider import Completion


def test_diagnostic_is_synthetic_and_compares_schema_transport():
    report = asyncio.run(run_diagnostic())
    assert report["source"] == "synthetic_cases_only"
    assert report["writes"] == {"graph_revision": 0, "fact_publication": 0}
    for case in report["cases"].values():
        assert set(case["variants"]) == {"duplicated", "native"}
        assert all(variant["status"] == "completed" for variant in case["variants"].values())
    duplicated = report["cases"]["negation"]["variants"]["duplicated"]
    native = report["cases"]["negation"]["variants"]["native"]
    assert duplicated["request"]["tokens"] > native["request"]["tokens"]
    assert duplicated["request"]["effective_transport"] == "native"
    assert native["request"]["effective_transport"] == "native"
    assert "没有" in duplicated["extracted_json"]["claims"][0]["statement"]


def test_diagnostic_reports_only_safe_rate_limit_metadata():
    class Limited:
        async def complete(self, *args, **kwargs):
            raise AdmissionRejected("private provider response", retry_after_s=12.5,
                                    exact_hint=True, category="rate_limited")

    report = asyncio.run(run_diagnostic(Limited()))
    item = report["cases"]["negation"]["variants"]["native"]
    assert item["error_class"] == "rate_limited"
    assert item["rate_limit"] == {"category": "rate_limited", "retry_after_s": 12.5,
                                   "exact_hint": True}
    assert "private provider response" not in str(report)


def test_diagnostic_reports_allowlisted_success_rate_headers():
    class Limited:
        async def complete(self, *args, **kwargs):
            return Completion(text='{"claims": []}', served_provider='x', served_model='m',
                               input_tokens=1, output_tokens=1,
                               rate_limits={"x-ratelimit-remaining-tokens": "9", "secret": "no"})

    report = asyncio.run(run_diagnostic(Limited()))
    item = report["cases"]["negation"]["variants"]["native"]
    assert item["rate_limit"] == {"x-ratelimit-remaining-tokens": "9"}
    assert "secret" not in str(report)


def test_diagnostic_passes_budget_and_uses_provider_model_transport():
    class Native:
        _model = "openai/gpt-oss-20b"

        def schema_transport(self, model):
            assert model == self._model
            return "native"

        async def complete(self, prompt, **kwargs):
            assert kwargs["model"] == self._model
            assert kwargs["max_output_tokens"] == 77
            data = json.loads(prompt.split("INPUT DATA:", 1)[-1])
            body = {"claims": [{"statement": data["source"], "passage_ids": ["p0"]}]}
            return Completion(text=json.dumps(body, ensure_ascii=False), served_provider="x",
                              served_model=self._model, input_tokens=1, output_tokens=1)

    report = asyncio.run(run_diagnostic(Native(), output_tokens=77))
    native = report["cases"]["negation"]["variants"]["native"]
    assert native["request"]["effective_transport"] == "native"
    assert native["served_provider"] == "x"
    assert native["served_model"] == "openai/gpt-oss-20b"
