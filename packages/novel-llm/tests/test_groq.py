"""GroqProvider contract against a mocked OpenAI-compatible transport."""

import json

import httpx
import pytest

from novel_llm.groq import GroqProvider, GroqRequestError, strict_schema


async def test_complete_records_groq_identity_usage_and_json_contract():
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    await provider._client.aclose()
    schema = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "string"}}}, "required": ["facts"]}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 8192
        assert payload["reasoning_effort"] == "low"
        assert payload["include_reasoning"] is False
        assert payload["response_format"] == {"type": "json_schema", "json_schema": {
            "name": "completion", "strict": True, "schema": strict_schema(schema),
        }}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"facts":[]}'}}],
            "model": "openai/gpt-oss-120b",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 10}},
        })

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle),
                                         headers={"Authorization": "Bearer test-key"})
    try:
        completion = await provider.complete("source", system="stable", json_schema=schema)
    finally:
        await provider.aclose()
    assert completion.text == '{"facts":[]}'
    assert completion.served_provider == "groq"
    assert completion.served_model == "openai/gpt-oss-120b"
    assert (completion.input_tokens, completion.output_tokens, completion.cache_read_tokens) == (100, 20, 10)


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
async def test_error_keeps_safe_code_without_body_and_does_not_retry(code, expected):
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    await provider._client.aclose()
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"code": code,
            "message": "private chapter secret", "failed_generation": "private chapter secret"}})
    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(GroqRequestError) as caught:
            await provider.complete("source", json_mode=True)
        assert caught.value.provider_code == expected
        assert "private chapter secret" not in str(caught.value)
        assert len(calls) == 1
        assert json.loads(calls[0].content)["response_format"] == {"type": "json_object"}
    finally:
        await provider.aclose()


async def test_output_token_exhaustion_is_not_returned_as_a_completion():
    provider = GroqProvider(model="openai/gpt-oss-120b", api_key="test-key")
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{
            "message": {"content": ""}, "finish_reason": "length"}]})))
    try:
        with pytest.raises(RuntimeError, match="provider output limit reached"):
            await provider.complete("source", json_mode=True)
    finally:
        await provider.aclose()
