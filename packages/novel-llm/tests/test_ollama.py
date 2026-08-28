"""Exercise schema delivery through the real adapter and batch path, without a server."""

import json

import httpx
import pytest

from novel_llm.ollama import OllamaProvider

SCHEMA = {"type": "object", "properties": {"value": {"type": "string", "minLength": 1}},
          "required": ["value"]}


@pytest.mark.parametrize("streaming", [False, True])
async def test_batch_delivers_schema_in_buffered_and_streaming_modes(streaming):
    provider = OllamaProvider(host="http://test", model="local-model")
    await provider._client.aclose()
    previews = []

    async def sink(text):
        previews.append(text)

    if streaming:
        provider.stream_sink = sink

    def handle(request):
        payload = json.loads(request.content)
        assert payload["format"] == SCHEMA
        assert payload["options"] == {"temperature": 0}
        assert payload["stream"] is streaming
        assert payload["model"] == "requested-model"
        body = {"message": {"content": '{"value":"known"}'}, "done": True,
                "prompt_eval_count": 10, "eval_count": 5}
        return httpx.Response(200, content=json.dumps(body) + "\n")

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        batch = await provider.batch_submit([{
            "id": "extract", "prompt": "source", "system": "instructions",
            "model": "requested-model", "json_schema": SCHEMA,
        }])
        result = (await provider.batch_poll(batch))[0]
        assert result["error"] is None
        assert result["output"] == '{"value":"known"}'
        assert result["served_model"] == "requested-model"
        assert bool(previews) is streaming
    finally:
        await provider.aclose()


@pytest.mark.parametrize("json_mode", [False, True])
async def test_ordinary_calls_keep_their_existing_payload(json_mode):
    provider = OllamaProvider(host="http://test", model="model")
    await provider._client.aclose()

    def handle(request):
        payload = json.loads(request.content)
        assert payload.get("format") == ("json" if json_mode else None)
        assert "options" not in payload
        return httpx.Response(200, json={"message": {"content": "{}"}})

    provider._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    try:
        assert (await provider.complete("source", json_mode=json_mode)).text == "{}"
    finally:
        await provider.aclose()
