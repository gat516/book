"""Regression coverage for the production records LLM contract."""

from types import SimpleNamespace

from fixtures import make_config

from pipeline.records import check_records, parse_records
from pipeline.stages.records import _discovery_prompt, _key
from pipeline.records_prompts import DISCOVERY_SYSTEM
from pipeline.records_publish import _warning_count


def _ctx(provider_id=None):
    return SimpleNamespace(cfg=make_config(), provider_id=provider_id, model_override=None)


def test_discovery_contract_names_real_child_tags_and_rejects_generic_field_shape():
    assert "<field>value</field>" not in DISCOVERY_SYSTEM
    assert "Never emit <field>" in DISCOVERY_SYSTEM
    assert "generic" in DISCOVERY_SYSTEM
    for tags in (
        "what,who,outcome,told", "speaker,act,addressee,content,accepted",
        "character,goal,knows,unknown,condition,location", "side_a,side_b,kind,polarity",
        "promiser,promisee,promised,status", "character,ability,effect", "fact,scope", "entity,is,of",
    ):
        for tag in tags.split(","):
            assert tag in DISCOVERY_SYSTEM


def test_discovery_prompt_offers_only_exact_passage_ids():
    payload = {"ontology": {}, "passages": {"p0_aabb": "text", "p190_deadbeef": "other"}}
    prompt = _discovery_prompt(payload, list(payload["passages"]))
    assert "p0_aabb" in prompt and "p190_deadbeef" in prompt
    assert "p001" not in prompt
    assert "authoritative_passage_ids" in prompt


def test_parse_records_recovers_compact_typed_fragment_list():
    passages = {"p0_hash": "安若素在碎星滩带队寻找星莲。", "p1_hash": "凌峰答应协助寻找。"}
    reply = '''<STATE character="安若素" goal="寻找星莲" location="碎星滩" evidence="p0_hash"/>
<PROMISE promiser="凌峰" promisee="安若素" promised="协助寻找" status="答应" evidence="p1_hash"/>'''

    parsed = parse_records(reply, passages)

    assert parsed["document"] == "recovered_fragments"
    assert parsed["counts"] == {"records": 2, "usable": 2}
    assert parsed["records"][0]["type"] == "STATE"
    assert parsed["records"][0]["fields"]["character"] == "安若素"
    assert parsed["records"][1]["fields"]["promised"] == "协助寻找"


def test_parse_records_rejects_empty_compact_record():
    parsed = parse_records('<STATE evidence="p0_hash"/>', {"p0_hash"})
    assert parsed["counts"] == {"records": 1, "usable": 0}
    assert "contains no non-empty fields" in parsed["records"][0]["issues"]


def test_records_cache_key_changes_when_system_or_prompt_changes():
    ctx = _ctx()
    payload = {"passages": {"p0_a": "text"}}
    base = _key("discovery", "sha256:x", payload, ctx, prompt="input", system=DISCOVERY_SYSTEM)
    changed_system = _key("discovery", "sha256:x", payload, ctx, prompt="input", system=DISCOVERY_SYSTEM + " revised")
    changed_prompt = _key("discovery", "sha256:x", payload, ctx, prompt="input revised", system=DISCOVERY_SYSTEM)
    assert base != changed_system
    assert base != changed_prompt
    assert base != _key("discovery", "sha256:x", payload, _ctx("openrouter"),
                        prompt="input", system=DISCOVERY_SYSTEM)


def test_hashed_passage_id_is_accepted_and_used_for_grounding_window():
    passages = {
        "p0_abcdef": "前文。",
        "p14_abcdef": "凌峰知道星莲的位置。",
        "p21_abcdef": "后文。",
    }
    parsed = parse_records(
        '<records><record type="STATE" evidence="p14_abcdef">'
        '<character>凌峰</character><knows>星莲的位置</knows>'
        '</record></records>',
        passages,
    )
    assert parsed["records"][0]["usable"] is True
    assert check_records(parsed["records"], passages) == {"dropped": [], "kept": 1}


def test_bare_generic_fields_are_rejected_without_guessing_pairs():
    parsed = parse_records(
        '<records><record type="STATE" evidence="p0_hash">'
        '<field>character</field><field>凌峰</field>'
        '</record></records>',
        {"p0_hash": "凌峰"},
    )
    record = parsed["records"][0]
    assert record["usable"] is False
    assert record["fields"] == {}
    assert record["issues"] == [
        "generic <field> is missing a name attribute",
        "generic <field> is missing a name attribute",
    ]


def test_unknown_or_duplicate_named_fields_fail_closed():
    parsed = parse_records(
        '<records><record type="STATE" evidence="p0_hash">'
        '<character>凌峰</character><character>另一个人</character><mood>高兴</mood>'
        '</record></records>',
        {"p0_hash": "凌峰"},
    )
    record = parsed["records"][0]
    assert record["usable"] is False
    assert "duplicate field 'character'" in record["issues"]
    assert "fields not defined for STATE: ['mood']" in record["issues"]


def test_warning_count_includes_parser_rejections_and_malformed_fragments():
    result = {
        "records": [{"usable": True}, {"usable": False}, {"usable": False}],
        "problems": [{"problem": "malformed fragment"}],
        "checks": {"dropped": []},
        "resolution": {"problems": ["one unresolved diagnostic"]},
    }
    assert _warning_count(result) == 4
