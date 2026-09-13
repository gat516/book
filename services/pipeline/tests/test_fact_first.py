import json

import pytest

from pipeline.benchmark_fact_first import (
    CLAIM_CEILINGS,
    DiscoveryResponse,
    DISCOVERY_SYSTEM,
    DISCOVERY_SUPPORT_FIRST_SYSTEM,
    ExtractionResponse,
    NORMALIZATION_SYSTEM,
    NORMALIZATION_SUPPORT_FIRST_SYSTEM,
    run_experiment,
    validate_discovery,
    validate_extraction,
    validate_translation,
)
from pipeline.llm.provider import Completion
from novel_llm.provider import TruncatedOutput


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def count_request_tokens(self, prompt, **kwargs):
        del kwargs
        return len(prompt) + 7

    async def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Completion(
            text=response if isinstance(response, str) else json.dumps(response, ensure_ascii=False),
            served_provider="fake",
            served_model="fake-model",
            input_tokens=len(prompt) + 7,
            output_tokens=20,
            cache_read_tokens=3,
        )


def dataset(tmp_path):
    path = tmp_path / "reviewed.json"
    source = "王林进入青云宗。\n青云宗的王长老点了点头。"
    path.write_text(json.dumps({
        "chapters": [{"chapter": 1, "source": source, "translation": ""}],
        "mentions": [
            {"chapter": 1, "surface": "王林", "kind": "character"},
            {"chapter": 1, "surface": "青云宗", "kind": "place"},
        ],
    }))
    return path, source


def extraction():
    return (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="王林" aliases="" '
        'english="Wang Lin" kind="character" evidence="p001"/></entities>'
        '<facts><fact claim="c1" subject="e1" attribute="visited" value="进入青云宗" '
        'evidence="p001"/></facts>'
        '<relations></relations><events></events></knowledge>'
    )


def discovery():
    return '<claims><claim id="c1" evidence="p001" context="">王林进入青云宗</claim></claims>'


def translation(second: bool = True):
    rows = ['<p id="p001">Wang Lin entered the Azure Cloud Sect.</p>']
    if second:
        rows.append('<p id="p002">The elder of the Azure Cloud Sect nodded.</p>')
    return "<translation>" + "".join(rows) + "</translation>"


def test_support_first_prompts_do_not_filter_by_narrative_importance():
    assert "materially explain the chapter's" in DISCOVERY_SYSTEM
    assert "Do not filter claims by narrative significance" in DISCOVERY_SUPPORT_FIRST_SYSTEM
    assert "temporary" in DISCOVERY_SUPPORT_FIRST_SYSTEM
    assert "gesture" in DISCOVERY_SUPPORT_FIRST_SYSTEM
    assert "intention or plan" in DISCOVERY_SUPPORT_FIRST_SYSTEM
    assert "figurative language" in DISCOVERY_SUPPORT_FIRST_SYSTEM
    assert "supported claim is not rejected" in NORMALIZATION_SUPPORT_FIRST_SYSTEM
    assert "fact-versus-event" in NORMALIZATION_SUPPORT_FIRST_SYSTEM
    assert "apparently unimportant" not in NORMALIZATION_SUPPORT_FIRST_SYSTEM


async def test_fresh_success_is_exactly_three_calls(tmp_path):
    path, source = dataset(tmp_path)
    provider = FakeProvider([
        discovery(),
        extraction(),
        translation(),
    ])
    report = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
        cached_input_discount=0.5,
    )
    assert report["status"] == "completed"
    assert len(provider.calls) == 3
    assert report["run_metrics"]["logical_api_calls"] == 3
    assert report["run_metrics"]["source_copies_sent"] == 2
    assert report["discovery"]["accepted"][0]["evidence"][0]["text"] == "王林进入青云宗。"
    assert report["discovery"]["claim_ceiling_reached"] is False
    assert report["extraction"]["name_map"] == {"王林": "Wang Lin"}
    assert report["extraction"]["accepted"]["facts"][0]["evidence"] == [{
        "id": "p001", "text": "王林进入青云宗。", "char_start": 0, "char_end": 8,
    }]
    claim_flow = report["extraction"]["claim_diagnostics"][0]
    assert claim_flow["claim"]["claim_id"] == "c1"
    assert claim_flow["decision"]["verdict"] == "supported"
    assert {row["section"] for row in claim_flow["accepted_records"]} == {"entities", "facts"}
    assert claim_flow["validation_rejections"] == []
    assert report["extraction"]["supported_claims_without_records"] == []
    assert report["translation"]["paragraphs_preserved"]
    assert report["source_hash"]
    assert report["source_characters"] == len(source)
    assert report["source_baseline"]["estimated"] is True
    assert report["run_metrics"]["gross_context_multiplier"] is None
    assert report["run_metrics"]["provider_input_vs_estimated_baseline_multiplier"] > 0
    assert report["run_metrics"]["estimated_context_multiplier"] > 0


async def test_translation_may_use_a_separate_model(tmp_path):
    path, _ = dataset(tmp_path)
    provider = FakeProvider([
        discovery(),
        extraction(),
        translation(),
    ])
    report = await run_experiment(
        provider, provider_name="fake", model="extract-model",
        translation_model="translation-model", dataset_path=path,
    )
    assert report["status"] == "completed"
    assert provider.calls[0][1]["model"] == "extract-model"
    assert provider.calls[1][1]["model"] == "extract-model"
    assert provider.calls[2][1]["model"] == "translation-model"
    assert provider.calls[2][1]["json_mode"] is False
    assert provider.calls[2][1]["json_schema"] is None
    assert report["requested_translation_model"] == "translation-model"


def test_bad_fact_is_rejected_without_losing_names(tmp_path):
    path, _ = dataset(tmp_path)
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="王林" aliases="" '
        'english="Wang Lin" kind="character" evidence="p001"/></entities>'
        '<facts><fact claim="c1" subject="missing" attribute="status" value="安全" '
        'evidence="p001"/></facts>'
        '<relations></relations><events></events></knowledge>'
    )
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    result = validate_extraction(body, case, _validated_discovery(case))
    assert result["name_map"] == {"王林": "Wang Lin"}
    assert result["accepted"]["facts"] == []
    assert result["rejected"][0]["section"] == "facts"
    assert result["schema_limitations"][0]["claim_id"] == "c1"
    rejection = result["claim_diagnostics"][0]["validation_rejections"][0]
    assert rejection["section"] == "facts"
    assert "unknown entity" in rejection["reason"]


def test_supported_claim_without_graph_record_is_reported(tmp_path):
    path, _ = dataset(tmp_path)
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" '
        'evidence="p001"/></decisions><entities></entities><facts></facts>'
        '<relations></relations><events></events></knowledge>'
    )
    result = validate_extraction(body, case, _validated_discovery(case))
    assert result["supported_claims_without_records"] == ["c1"]
    assert result["claim_diagnostics"][0]["accepted_records"] == []
    assert result["claim_diagnostics"][0]["decision"]["verdict"] == "supported"


async def test_malformed_discovery_stops_before_normalization(tmp_path):
    path, _ = dataset(tmp_path)

    class Malformed(FakeProvider):
        async def complete(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return Completion(text="not json", served_provider="fake", served_model="fake-model")

    provider = Malformed([])
    report = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
    )
    assert report["status"] == "failed"
    assert len(provider.calls) == 1
    assert report["attempts"][0]["response"] == "not json"
    assert "translation" not in report


async def test_translation_only_reuses_accepted_extraction(tmp_path):
    path, _ = dataset(tmp_path)
    discovered = await run_experiment(
        FakeProvider([discovery()]), provider_name="fake", model="fake-model",
        dataset_path=path, stage="discover",
    )
    prior = await run_experiment(
        FakeProvider([extraction()]), provider_name="fake", model="fake-model",
        dataset_path=path, stage="normalize", reuse_artifact=discovered,
    )
    provider = FakeProvider([translation()])
    report = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="translate", reuse_artifact=prior,
    )
    assert report["status"] == "completed"
    assert len(provider.calls) == 1
    assert report["run_metrics"]["logical_api_calls"] == 1
    assert report["extraction"]["name_map"] == {"王林": "Wang Lin"}
    assert report["effective_pipeline_metrics"]["logical_api_calls"] == 3


async def test_matching_stage_artifact_uses_zero_api_calls(tmp_path):
    path, _ = dataset(tmp_path)
    first_provider = FakeProvider([discovery()])
    first = await run_experiment(
        first_provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover",
    )
    second_provider = FakeProvider([])
    second = await run_experiment(
        second_provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover", reuse_artifact=first,
    )
    assert second["status"] == "completed"
    assert second["run_metrics"]["logical_api_calls"] == 0
    assert second["run_metrics"]["reused_calls"] == 1
    assert second_provider.calls == []


async def test_baseline_replays_legacy_request_identity(tmp_path):
    path, _ = dataset(tmp_path)
    first = await run_experiment(
        FakeProvider([discovery()]), provider_name="fake", model="fake-model",
        dataset_path=path, stage="discover",
    )
    # Simulate the saved pre-variant artifact: its request digest must remain reusable.
    first.pop("variants", None)
    first.pop("artifact_label", None)
    provider = FakeProvider([])
    replay = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover", reuse_artifact=first,
    )
    assert replay["run_metrics"]["logical_api_calls"] == 0
    assert replay["attempts"][0]["transport"] == "reuse"


async def test_revised_discovery_can_feed_both_normalization_variants(tmp_path):
    path, _ = dataset(tmp_path)
    discovered = await run_experiment(
        FakeProvider([discovery()]), provider_name="fake", model="fake-model",
        dataset_path=path, stage="discover", discovery_variant="revised",
    )
    for variant in ("baseline", "support_first"):
        report = await run_experiment(
            FakeProvider([extraction()]), provider_name="fake", model="fake-model",
            dataset_path=path, stage="normalize", normalization_variant=variant,
            discovery_variant="revised", reuse_artifact=discovered,
        )
        assert report["status"] == "completed"
        assert report["discovery"]["accepted"]


async def test_invalid_completed_response_is_not_reused(tmp_path):
    path, _ = dataset(tmp_path)

    class Malformed(FakeProvider):
        async def complete(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return Completion(text="not json", served_provider="fake", served_model="fake-model")

    failed = await run_experiment(
        Malformed([]), provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover",
    )
    assert failed["attempts"][0]["validation_status"] == "rejected"
    provider = FakeProvider([discovery()])
    retried = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover", reuse_artifact=failed,
    )
    assert retried["status"] == "completed"
    assert retried["run_metrics"]["logical_api_calls"] == 1
    assert len(provider.calls) == 1


async def test_provider_failure_has_unknown_usage_and_is_preserved(tmp_path):
    path, _ = dataset(tmp_path)
    provider = FakeProvider([RuntimeError("API unavailable")])
    report = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
        stage="discover",
    )
    assert report["status"] == "failed"
    assert report["run_metrics"]["unknown_usage_attempts"] == 1
    assert report["run_metrics"]["gross_context_multiplier"] is None
    assert report["attempts"][0]["error"] == {
        "type": "RuntimeError", "message": "API unavailable",
    }


async def test_truncated_output_preserves_partial_usage_without_accepting_it(tmp_path):
    path, _ = dataset(tmp_path)
    partial = Completion(
        text="<claims><claim id=\"c1\"",
        served_provider="groq",
        served_model="openai/gpt-oss-120b",
        input_tokens=123,
        output_tokens=58,
        cache_read_tokens=19,
    )
    truncated = TruncatedOutput()
    truncated.partial_completion = partial
    truncated.finish_reason = "length"
    provider = FakeProvider([truncated])
    report = await run_experiment(
        provider, provider_name="groq", model="fake-model", dataset_path=path,
        stage="discover",
    )
    attempt = report["attempts"][0]
    assert report["status"] == "failed"
    assert "validation_status" not in attempt
    assert attempt["response"] == partial.text
    assert attempt["served_provider"] == "groq"
    assert attempt["served_model"] == "openai/gpt-oss-120b"
    assert attempt["usage"] == {
        "input_tokens": 123,
        "output_tokens": 58,
        "cache_read_tokens": 19,
        "cache_write_tokens": 0,
    }
    assert report["run_metrics"]["unknown_usage_attempts"] == 0
    assert report["run_metrics"]["provider_reported_input_tokens"] == 123
    assert report["run_metrics"]["provider_reported_output_tokens"] == 58


async def test_incomplete_translation_is_rejected(tmp_path):
    path, _ = dataset(tmp_path)
    provider = FakeProvider([discovery(), extraction(), translation(second=False)])
    report = await run_experiment(
        provider, provider_name="fake", model="fake-model", dataset_path=path,
    )
    assert report["status"] == "failed"
    assert report["attempts"][2]["validation_status"] == "rejected"
    assert "passage IDs differ" in report["error"]["message"]


async def test_incomplete_normalization_decision_coverage_fails(tmp_path):
    path, _ = dataset(tmp_path)
    incomplete = (
        '<knowledge><decisions></decisions><entities></entities><facts></facts>'
        '<relations></relations><events></events></knowledge>'
    )
    report = await run_experiment(
        FakeProvider([discovery(), incomplete]),
        provider_name="fake", model="fake-model", dataset_path=path,
    )
    assert report["status"] == "failed"
    assert report["attempts"][1]["validation_status"] == "rejected"
    assert "every discovered claim" in report["error"]["message"]


def test_schema_is_closed_for_hosted_strict_mode():
    schema = ExtractionResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "decisions", "entities", "facts", "relations", "events"
    }
    fact_schema = schema["$defs"]["FactProposal"]
    assert "value_en" not in fact_schema["properties"]
    assert "args" in schema["$defs"]["EventProposal"]["properties"] or (
        "arguments" in schema["$defs"]["EventProposal"]["properties"]
    )
    assert "value_en" not in NORMALIZATION_SYSTEM
    assert 'source="姓名"' not in NORMALIZATION_SYSTEM


def test_discovery_rejects_missing_evidence_locally(tmp_path):
    path, _ = dataset(tmp_path)
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = discovery().replace('evidence="p001"', 'evidence="p999"')
    result = validate_discovery(body, case)
    assert result["accepted"] == []
    assert "does not exist" in result["rejected"][0]["reason"]


def test_discovery_reports_the_hard_claim_ceiling(tmp_path):
    path, _ = dataset(tmp_path)
    case = {
        "source": "王林进入青云宗。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = "<claims>" + "".join(
        f'<claim id="c{index}" evidence="p001" context="">命题{index}</claim>'
        for index in range(1, 31)
    ) + "</claims>"
    result = validate_discovery(body, case)
    assert len(result["accepted"]) == 30
    assert result["claim_ceiling"] == 30
    assert result["claim_ceiling_reached"] is True
    assert result["potentially_incomplete"] is True


def test_discovery_drops_one_malformed_claim_tag_not_the_whole_batch(tmp_path):
    # Regression: a single stray `id="p019"` (a passage ID copied into the claim-ID
    # slot -- observed from a real Groq gpt-oss-120b response) used to fail the
    # entire discovery response and lose every other, well-formed claim with it.
    path, _ = dataset(tmp_path)
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<claims>'
        '<claim id="c1" evidence="p001" context="">王林进入青云宗。</claim>'
        '<claim id="p019" evidence="p002" context="">青云宗的王长老点了点头。</claim>'
        '</claims>'
    )
    result = validate_discovery(body, case)
    assert [row["claim_id"] for row in result["accepted"]] == ["c1"]
    assert result["malformed_claim_tags"] == 1
    assert "string_pattern_mismatch" in result["rejected"][0]["reason"] or (
        "pattern" in result["rejected"][0]["reason"]
    )


def test_discovery_drops_a_claim_missing_the_context_attribute(tmp_path):
    # Regression: a model omitting the (optional, empty-when-unused) context=""
    # attribute entirely used to fail the whole batch instead of just that claim.
    path, _ = dataset(tmp_path)
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<claims>'
        '<claim id="c1" evidence="p001">王林进入青云宗。</claim>'
        '<claim id="c2" evidence="p002" context="">青云宗的王长老点了点头。</claim>'
        '</claims>'
    )
    result = validate_discovery(body, case)
    assert [row["claim_id"] for row in result["accepted"]] == ["c2"]
    assert result["malformed_claim_tags"] == 1


def test_discovery_claim_ceiling_is_per_variant():
    # support_first is recall-first: a 56-passage chapter legitimately yields ~45
    # claims, which the baseline's 30 would reject outright.
    case = {"source": "王林进入青云宗。", "ontology": {"kinds": ["character", "place"]}}
    body = "<claims>" + "".join(
        f'<claim id="c{index}" evidence="p001" context="">命题{index}</claim>'
        for index in range(1, 46)
    ) + "</claims>"
    with pytest.raises(ValueError, match="ceiling is 30"):
        validate_discovery(body, case)
    result = validate_discovery(body, case, CLAIM_CEILINGS["support_first"])
    assert len(result["accepted"]) == 45
    assert result["claim_ceiling_reached"] is False


def test_discovery_schema_is_closed_for_hosted_strict_mode():
    schema = DiscoveryResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["claims"]


def _validated_discovery(case):
    return validate_discovery(discovery(), case)


def test_normalization_rejects_unknown_claim_and_passage_outside_supplied_package():
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<knowledge><decisions>'
        '<decision claim="c999" verdict="supported" evidence="p001"/>'
        '<decision claim="c1" verdict="supported" evidence="p002"/>'
        '</decisions><entities>'
        '<entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" '
        'kind="character" evidence="p002"/>'
        '</entities><facts></facts><relations></relations><events></events></knowledge>'
    )
    with pytest.raises(ValueError, match="every discovered claim"):
        validate_extraction(body, case, _validated_discovery(case))


def test_normalization_preserves_event_roles_and_rejects_unknown_role_refs():
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/>'
        '</decisions><entities>'
        '<entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" '
        'kind="character" evidence="p001"/>'
        '<entity claim="c1" id="e2" source="青云宗" aliases="" english="Azure Cloud Sect" '
        'kind="place" evidence="p001"/>'
        '</entities><facts></facts><relations></relations><events>'
        '<event claim="c1" action="enter" args="actor:e1,location:e2,guide:e1" '
        'evidence="p001"/>'
        '<event claim="c1" action="enter" args="actor:e1,target:e404,location:e2" '
        'evidence="p001"/>'
        '</events></knowledge>'
    )
    result = validate_extraction(body, case, _validated_discovery(case))
    assert result["accepted"]["events"][0]["arguments"] == [
        {"role": "actor", "entity_id": "e1"},
        {"role": "location", "entity_id": "e2"},
        {"role": "guide", "entity_id": "e1"},
    ]
    assert len(result["accepted"]["events"]) == 1
    assert any("unknown entity" in row["reason"] for row in result["rejected"])


def test_normalization_cannot_use_another_claims_passage():
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p001" context="">王林进入青云宗</claim>'
        '<claim id="c2" evidence="p002" context="">王长老点头</claim></claims>',
        case,
    )
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/>'
        '<decision claim="c1" verdict="supported" evidence="p002"/>'
        '<decision claim="c2" verdict="supported" evidence="p002"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" '
        'kind="character" evidence="p002"/></entities>'
        '<facts><fact claim="c1" subject="e1" attribute="visited" value="青云宗" evidence="p002"/>'
        '<fact claim="c1" subject="e1" attribute="entered" value="青云宗" evidence="p001,p002"/>'
        '</facts><relations></relations><events></events></knowledge>'
    )
    result = validate_extraction(body, case, discovered)
    assert [row["claim_id"] for row in result["accepted"]["decisions"]] == ["c1", "c2"]
    # The model's entity citation (p002, another claim's passage) is ignored; evidence
    # is derived from c1's own package only.
    [entity] = result["accepted"]["entities"]
    assert entity["evidence_ids"] == ["p001"] and entity["evidence_source"] == "derived"
    # A fact citing only another claim's passage is still rejected...
    assert any(
        row["section"] == "facts" and "claim evidence/context" in row["reason"]
        for row in result["rejected"]
    )
    # ...while one with a valid citation keeps it and records the dropped one.
    [fact] = result["accepted"]["facts"]
    assert fact["evidence_ids"] == ["p001"]
    assert fact["citation_corrections"]["dropped"] == ["p002"]


def test_entity_needs_no_model_citation_and_many_mentions_do_not_fail():
    # Regression: nemotron cited every passage naming Ling Feng (7 > the old cap of 4)
    # and the entity -- plus every fact about him -- was rejected.
    case = {
        "source": "王林进入青云宗。\n王林拜见长老。\n王林离开。",
        "ontology": {"kinds": ["character", "place"]},
    }
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p001,p002,p003" context="">王林进出青云宗</claim></claims>',
        case,
    )
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" '
        'kind="character"/></entities>'
        '<facts></facts><relations></relations><events></events></knowledge>'
    )
    result = validate_extraction(body, case, discovered)
    [entity] = result["accepted"]["entities"]
    assert entity["evidence_ids"] == ["p001", "p002", "p003"]


def test_discovery_accepts_a_markdown_fenced_response():
    # Regression: Groq gpt-oss-120b wrapped valid XML in ```xml ... ``` and the whole
    # response failed to parse at column 0.
    case = {"source": "王林进入青云宗。", "ontology": {"kinds": ["character", "place"]}}
    body = "```xml\n" + discovery() + "\n```"
    result = validate_discovery(body, case)
    assert [row["claim_id"] for row in result["accepted"]] == ["c1"]


def test_entity_is_rejected_when_no_package_passage_names_it():
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p002" context="">王长老点头</claim></claims>', case,
    )
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p002"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" '
        'kind="character"/></entities>'
        '<facts></facts><relations></relations><events></events></knowledge>'
    )
    result = validate_extraction(body, case, discovered)
    assert result["accepted"]["entities"] == []
    assert "no passage in the claim's package names the entity" in result["rejected"][0]["reason"]


def test_translation_missing_required_name_is_reported_for_evaluation():
    source = "王林进入青云宗。"
    result = validate_translation(
        "<translation><p id=\"p001\">He entered the sect.</p></translation>",
        source,
        {"王林": "Wang Lin"},
    )
    assert result["missing_english_names"] == ["Wang Lin"]


def test_contradicted_claim_cannot_emit_graph_records():
    case = {
        "source": "王林进入青云宗。\n青云宗的王长老点了点头。",
        "ontology": {"kinds": ["character", "place"]},
    }
    body = (
        '<knowledge><decisions><decision claim="c1" verdict="contradicted" evidence="p001"/>'
        '</decisions><entities><entity claim="c1" id="e1" source="王林" aliases="" '
        'english="Wang Lin" kind="character" evidence="p001"/></entities>'
        '<facts></facts><relations></relations><events></events></knowledge>'
    )
    result = validate_extraction(body, case, _validated_discovery(case))
    assert result["accepted"]["decisions"][0]["verdict"] == "contradicted"
    assert result["accepted"]["entities"] == []
    assert any("not supported" in row["reason"] for row in result["rejected"])


def test_wrong_existing_subject_is_rejected_with_valid_rows_preserved():
    case = {"source": "九殒神剑发光。\n机械蜘蛛刻着威尔森徽记。",
            "ontology": {"kinds": ["item"]}}
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p001" context="">九殒神剑发光</claim>'
        '<claim id="c2" evidence="p002" context="">机械蜘蛛刻着威尔森徽记</claim></claims>', case)
    result = validate_extraction(
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/>'
        '<decision claim="c2" verdict="supported" evidence="p002"/></decisions>'
        '<entities><entity claim="c1" id="e8" source="九殒神剑" aliases="" '
        'english="Divine Sword" kind="item" evidence="p001"/></entities>'
        '<facts><fact claim="c2" subject="e8" attribute="emblem" value="Wilson" evidence="p002"/>'
        '</facts><relations/><events>'
        '<event claim="c1" action="glow" args="actor:e8" evidence="p001"/>'
        '<event claim="c1" action="glow" args="broken_argument" evidence="p001"/>'
        '</events></knowledge>', case, discovered)
    assert result["accepted"]["facts"] == []
    assert len(result["accepted"]["events"]) == 1
    assert len(result["rejected"]) == 2
    assert any("unanchored participant e8" in row["reason"] for row in result["rejected"])
    assert result["supported_but_unrepresented"] == ["c2"]


def test_duplicate_xml_attributes_remain_fatal():
    with pytest.raises(Exception, match="duplicate attribute"):
        validate_extraction('<knowledge><decisions/><entities/><facts/><relations>'
                            '<relation dst="e1" dst="e2"/></relations><events/></knowledge>',
                            {"source": "", "ontology": {"kinds": []}})


def test_bounded_context_recovers_pronouns_without_binding_nearby_weapon():
    from copy import deepcopy
    from pipeline.benchmark_fact_first import normalization_input, normalization_request

    case = {"chapter": 1, "source": "凌峰持九殒神剑。\n他伪装成诺顿。\n机械蜘蛛刻着徽记。",
            "ontology": {"kinds": ["character", "item"]}}
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p001" context="">凌峰持九殒神剑</claim>'
        '<claim id="c2" evidence="p002" context="">凌峰伪装成诺顿</claim>'
        '<claim id="c3" evidence="p003" context="">机械蜘蛛刻着徽记</claim></claims>', case)
    original = deepcopy(discovered)
    prepared = normalization_input(case, discovered, "support_first")
    assert discovered == original
    assert normalization_input(case, discovered, "baseline") == original
    assert normalization_input(case, prepared, "support_first") == prepared
    request = normalization_request(case, discovered, "test", 4096, variant="support_first")
    package = json.loads(request["prompt"].split("\n", 1)[1])
    assert package["claims"][1]["context_ids"] == ["p001", "p003"]
    assert len(package["passages"]) == 3
    xml = (
        '<knowledge><decisions>'
        '<decision claim="c1" verdict="supported" evidence="p001"/>'
        '<decision claim="c2" verdict="supported" evidence="p002"/>'
        '<decision claim="c3" verdict="supported" evidence="p003"/></decisions><entities>'
        '<entity claim="c1" id="e1" source="凌峰" aliases="" english="Ling Feng" kind="character" evidence="p001"/>'
        '<entity claim="c1" id="e2" source="九殒神剑" aliases="" english="Sword" kind="item" evidence="p001"/>'
        '</entities><facts>'
        '<fact claim="c2" subject="e1" attribute="disguised_as" value="诺顿" evidence="p002"/>'
        '<fact claim="c3" subject="e2" attribute="emblem" value="徽记" evidence="p003,p001"/>'
        '</facts><relations/><events/></knowledge>')
    result = validate_extraction(xml, case, prepared)
    assert [row["claim_id"] for row in result["accepted"]["facts"]] == ["c2"]
    assert result["accepted"]["facts"][0]["binding_evidence"]["e1"][0]["id"] == "p001"
    assert "unanchored participant e2" in result["rejected"][0]["reason"]


def test_context_is_chapter_local_and_cannot_rescue_unsupported_entity():
    from pipeline.benchmark_fact_first import normalization_input
    case = {"chapter": 1, "source": "我们要走了。\n姜梦月说。",
            "ontology": {"kinds": ["character"]}}
    discovery_rows = validate_discovery(
        '<claims><claim id="c1" evidence="p001" context="">姜梦月说要走了</claim></claims>', case)
    prepared = normalization_input(case, discovery_rows, "support_first")
    assert prepared["accepted"][0]["context_ids"] == ["p002"]
    xml = ('<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001"/></decisions>'
           '<entities><entity claim="c1" id="e1" source="姜梦月" aliases="" english="Jiang Mengyue" '
           'kind="character" evidence="p001"/></entities><facts/><relations/><events/></knowledge>')
    result = validate_extraction(xml, case, prepared)
    assert result["name_map"] == {"姜梦月": "Jiang Mengyue"}
    assert result["accepted"]["entities"][0]["binding_evidence"][0]["id"] == "p002"
    unsupported = validate_extraction(xml.replace('verdict="supported"', 'verdict="contradicted"'), case, prepared)
    assert unsupported["accepted"]["entities"] == []


def memory_discovery():
    return (
        '<claims><claim id="c1" evidence="p001,p002" context="" kind="wiki" '
        'subject="凌峰" predicate="can_use" object="七罪神权" basis="demonstrated" '
        'attribution="" temporal="unknown" qualifiers="">凌峰能使用七罪神权。</claim>'
        '<claim id="c2" evidence="p003" context="" kind="storyline" subject="凌峰" '
        'predicate="previously_stole" object="宝库" basis="stated" attribution="" '
        'temporal="prior" qualifiers="">凌峰此前曾盗取宝库。</claim></claims>'
    )


async def test_memory_candidates_prepare_evidence_without_inventing_acquisition_or_appending(tmp_path):
    from pipeline.benchmark_memory import candidates
    path = tmp_path / "midstory.json"
    source = "凌峰使用七罪神权。\n凌峰再次使用七罪神权。\n凌峰此前曾盗取宝库。"
    path.write_text(json.dumps({
        "chapters": [{"chapter": 4679, "source": source}],
        "mentions": [{"chapter": 4679, "surface": "凌峰", "kind": "character"}],
    }))
    provider = FakeProvider([memory_discovery(),
        '<knowledge><decisions><decision claim="c1" verdict="supported" evidence="p001,p002"/>'
        '<decision claim="c2" verdict="supported" evidence="p003"/></decisions>'
        '<entities><entity claim="c1" id="e1" source="凌峰" aliases="" english="Ling Feng" '
        'kind="character" evidence="p001"/></entities><facts>'
        '<fact claim="c1" subject="e1" attribute="can_use" value="七罪神权" evidence="p001,p002"/>'
        '</facts><relations/><events/></knowledge>'])
    report = await run_experiment(provider, provider_name="fake", model="fake-model",
        dataset_path=path, chapter=4679, stage="extract", discovery_variant="memory",
        normalization_variant="memory")
    assert report["status"] == "completed"
    result = report["memory_candidates"]
    assert len(result["wiki"]) == len(result["storyline"]) == 1
    ability = result["wiki"][0]
    assert len(ability["evidence"]) == 2  # two demonstrations, one capability
    assert ability["source_chapter"] == 4679
    assert ability["acquired_at_chapter"] is None
    assert ability["valid_from_chapter"] is None
    assert ability["subject_reference"]["entity_id"] is None
    assert ability["comparison_status"] == "not_compared"
    assert ability["semantic_review"] == "required"
    assert result["storyline"][0]["temporal"] == "prior"
    assert result["storyline"][0]["representation_status"] == "unrepresented"
    assert result["persistence"] == "not_implemented"
    # A later observation is not automatically a new ability, nor an automatic merge.
    later = candidates({"source": source, "chapter": 4685}, report["normalization_input"], report["extraction"])
    assert later["wiki"][0]["comparison_hint"] == ability["comparison_hint"]
    assert later["wiki"][0]["comparison_status"] == "not_compared"
    assert len(provider.calls) == 2
    assert "translation" not in report


def test_memory_metadata_is_closed_and_preserves_attribution_and_uncertainty():
    source = "冉若素说至少有两百只星源兽。"
    xml = ('<claims><claim id="c1" evidence="p001" context="" kind="wiki" '
           'subject="星源兽" predicate="estimated_count" object="至少两百只" basis="reported" '
           'attribution="冉若素" temporal="unknown" qualifiers="远处观察，估计">'
           '冉若素估计星源兽至少有两百只。</claim></claims>')
    result = validate_discovery(xml, {"source": source}, variant="memory")
    assert result["accepted"][0]["memory"]["basis"] == "reported"
    assert result["accepted"][0]["memory"]["attribution"] == "冉若素"
    assert result["accepted"][0]["memory"]["qualifiers"] == "远处观察，估计"
    assert validate_discovery(xml.replace('temporal="unknown"', 'temporal="newly_acquired"'),
                              {"source": source}, variant="memory")["accepted"] == []
    assert validate_discovery(xml.replace('evidence="p001"', 'evidence="p002"'),
                              {"source": source}, variant="memory")["accepted"] == []


def test_memory_prompts_have_separate_request_identity(tmp_path):
    # Relative prompt length was asserted here once; it is not a correctness property.
    from pipeline.benchmark_fact_first import (
        MEMORY_DISCOVERY_SYSTEM, _parser, _request_identity, discovery_request, load_case,
    )
    case = load_case(dataset(tmp_path)[0], 1)
    memory = discovery_request(case, "test", 4096, variant="memory")
    broad = discovery_request(case, "test", 4096, variant="support_first")
    assert _request_identity(memory, "fake") != _request_identity(broad, "fake")
    assert "First observed does not mean newly" in MEMORY_DISCOVERY_SYSTEM
    assert "grades" in MEMORY_DISCOVERY_SYSTEM
    assert "routine treatment" in MEMORY_DISCOVERY_SYSTEM
    args = _parser().parse_args(["--output", "unused.json"])
    assert args.discovery_variant == "atomic"
    assert args.normalization_variant == "assertion"
    assert args.stage == "extract"


def test_identical_entity_redeclaration_is_idempotent_and_conflict_fails_closed():
    # Regression: nemotron-3-super obeyed "reuse IDs across claims" by re-declaring the
    # same entity under every claim, and each repeat was rejected as a duplicate ID.
    case = {"source": "王林进入青云宗。\n王林拜见长老。", "ontology": {"kinds": ["character", "place"]}}
    discovered = validate_discovery(
        '<claims><claim id="c1" evidence="p001" context="">王林进入青云宗</claim>'
        '<claim id="c2" evidence="p002" context="">王林拜见长老</claim></claims>', case,
    )

    def body(second_english):
        return (
            '<knowledge><decisions>'
            '<decision claim="c1" verdict="supported" evidence="p001"/>'
            '<decision claim="c2" verdict="supported" evidence="p002"/></decisions>'
            '<entities><entity claim="c1" id="e1" source="王林" aliases="" english="Wang Lin" kind="character"/>'
            f'<entity claim="c2" id="e1" source="王林" aliases="" english="{second_english}" kind="character"/>'
            '</entities><facts><fact claim="c2" subject="e1" attribute="greeted" value="长老" evidence="p002"/>'
            '</facts><relations></relations><events></events></knowledge>'
        )

    result = validate_extraction(body("Wang Lin"), case, discovered)
    [entity] = result["accepted"]["entities"]
    assert entity["declared_by_claims"] == ["c1", "c2"]
    assert entity["evidence_ids"] == ["p001", "p002"]
    assert result["rejected"] == []
    assert len(result["accepted"]["facts"]) == 1

    result = validate_extraction(body("Wang Ling"), case, discovered)
    [entity] = result["accepted"]["entities"]
    assert entity["declared_by_claims"] == ["c1"]
    assert result["rejected"][0]["reason"] == "conflicting redeclaration of local entity ID"


def test_revalidation_replays_saved_response_without_a_model_call(tmp_path):
    from pipeline.benchmark_fact_first import revalidate_artifact
    path, source = dataset(tmp_path)
    case = {"source": source, "ontology": {"kinds": ["character", "place"]}}
    saved = {
        "chapter": 1, "status": "failed", "error": {"type": "ValueError", "message": "old validator"},
        "discovery": validate_discovery(discovery(), case),
        "attempts": [{"stage": "normalize", "status": "completed", "response": extraction()}],
    }
    replay = revalidate_artifact(saved, path)
    assert replay["status"] == "completed" and "error" not in replay
    assert replay["revalidation"]["original_status"] == "failed"
    assert len(replay["extraction"]["accepted"]["facts"]) == 1
    assert saved["status"] == "failed"  # the saved artifact itself is not rewritten


def test_case_carries_its_evaluation_split(tmp_path):
    from pipeline.benchmark_fact_first import load_case
    path, _ = dataset(tmp_path)
    assert load_case(path, 1)["split"] == "unlabelled"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "splits": {"regression": [1], "held_out": [2]}}))
    assert load_case(path, 1)["split"] == "regression"
