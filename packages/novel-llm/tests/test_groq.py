"""GroqProvider contract against a mocked OpenAI-compatible transport."""

import json

import httpx
import pytest
from types import SimpleNamespace

import novel_llm.hosted as hosted
from novel_llm.groq import GroqProvider, strict_schema
from novel_llm.provider import TruncatedOutput


async def test_complete_records_groq_identity_usage_and_json_contract(monkeypatch):
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    schema = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "string"}}}, "required": ["facts"]}

    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="openai/gpt-oss-120b", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"facts":[]}'))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                   prompt_tokens_details=SimpleNamespace(cached_tokens=10)),
            _hidden_params={"custom_llm_provider": "groq"})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    completion = await provider.complete("source", system="stable", json_schema=schema)
    assert completion.text == '{"facts":[]}'
    assert completion.served_provider == "groq"
    assert completion.served_model == "openai/gpt-oss-120b"
    assert (completion.input_tokens, completion.output_tokens, completion.cache_read_tokens) == (100, 20, 10)
    assert calls[0]["num_retries"] == 0 and calls[0]["fallbacks"] == []


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        GroqProvider(model="openai/gpt-oss-120b")


async def test_embed_is_not_supported():
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    try:
        with pytest.raises(NotImplementedError):
            await provider.embed(["text"])
    finally:
        await provider.aclose()


def test_strict_schema_closes_nested_objects_without_mutating_input():
    schema = {"type": "object", "properties": {"default": {"$ref": "#/$defs/Child"}},
              "$defs": {"Child": {"type": "object", "properties": {
                  "count": {"type": "integer", "default": 0}}}}}
    before = json.dumps(schema)
    result = strict_schema(schema)
    assert json.dumps(schema) == before
    assert result["required"] == ["default"]
    assert result["$defs"]["Child"]["required"] == ["count"]
    assert result["$defs"]["Child"]["additionalProperties"] is False
    assert "default" not in result["$defs"]["Child"]["properties"]["count"]


@pytest.mark.parametrize("code,expected", [("json_validate_failed", "json_validate_failed"),
                                           ("private chapter secret", "unclassified")])
async def test_error_keeps_safe_code_without_body_and_does_not_retry(monkeypatch, code, expected):
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    calls = []
    class SDKError(Exception):
        pass
    async def complete(**kwargs):
        calls.append(kwargs)
        raise SDKError("private chapter secret")
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    with pytest.raises(SDKError) as caught:
        await provider.complete("source", json_mode=True)
    assert "private chapter secret" in str(caught.value)
    assert len(calls) == 1


async def test_output_token_exhaustion_is_not_returned_as_a_completion(monkeypatch):
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    async def complete(**kwargs):
        return SimpleNamespace(model="openai/gpt-oss-120b", choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content=""))], usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    with pytest.raises(TruncatedOutput):
        await provider.complete("source", json_mode=True)


def test_strict_schema_merges_same_type_union_branches_and_reports_dropped_enums():
    # The shape passages.py builds for a vocabulary term: a known term, or a new one.
    # Groq rejects it outright (duplicate_primitive_types) because both branches are
    # strings and nothing distinguishes them token by token.
    schema = {"type": "object", "properties": {"attribute": {"anyOf": [
        {"type": "string", "enum": ["appearance", "ability"]},
        {"type": "string", "pattern": r"^[a-z][a-z0-9_]{1,39}$"},
    ]}}}
    widened: list[list] = []
    result = strict_schema(schema, widened)
    attribute = result["properties"]["attribute"]
    assert "anyOf" not in attribute, "same-type branches must collapse into one"
    assert attribute["type"] == "string"
    # Merging widens, never narrows: the pattern branch already admitted every enum value.
    assert "enum" not in attribute and "pattern" not in attribute
    assert widened == [["appearance", "ability"]], "the dropped enum must reach the prompt"


def test_strict_schema_keeps_nullable_unions_untouched():
    # {string} | {null} and {integer} | {null} are unambiguous -- a decoder can tell
    # them apart immediately -- so collapsing them would destroy the contract for no gain.
    schema = {"type": "object", "properties": {
        "passage_id": {"anyOf": [{"type": "string", "enum": ["p1"]}, {"type": "null"}]},
        "sentiment": {"anyOf": [{"type": "integer", "minimum": -1}, {"type": "null"}]}}}
    widened: list[list] = []
    result = strict_schema(schema, widened)
    assert result["properties"]["passage_id"]["anyOf"] == [
        {"type": "string", "enum": ["p1"]}, {"type": "null"}]
    assert result["properties"]["sentiment"]["anyOf"][1] == {"type": "null"}
    assert widened == []


def test_strict_schema_unions_enums_when_every_branch_is_closed():
    schema = {"type": "object", "properties": {"kind": {"anyOf": [
        {"type": "string", "enum": ["character", "place"]},
        {"type": "string", "enum": ["place", "sect"]}]}}}
    widened: list[list] = []
    result = strict_schema(schema, widened)
    # Both branches were closed, so the union stays closed and nothing is lost.
    assert result["properties"]["kind"] == {"type": "string",
                                            "enum": ["character", "place", "sect"]}
    assert widened == []


async def test_widened_enum_values_are_named_in_the_system_prompt(monkeypatch):
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    schema = {"type": "object", "properties": {"attribute": {"anyOf": [
        {"type": "string", "enum": ["appearance"]},
        {"type": "string", "pattern": r"^[a-z][a-z0-9_]{1,39}$"}]}},
        "required": ["attribute"]}

    async def complete(**payload):
        system = payload["messages"][0]["content"]
        assert "appearance" in system, "steering must survive the schema widening"
        sent = payload["response_format"]["json_schema"]["schema"]
        assert "anyOf" not in sent["properties"]["attribute"]
        return SimpleNamespace(model="openai/gpt-oss-120b", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"attribute":"appearance"}'))], usage={})
    monkeypatch.setattr(hosted, "litellm", SimpleNamespace(acompletion=complete))
    completion = await provider.complete("source", system="stable", json_schema=schema)
    assert completion.served_model == "openai/gpt-oss-120b"
