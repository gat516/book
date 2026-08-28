"""Schema guidance survives the existing gateway wire contract; no live gateway."""

import json
from types import SimpleNamespace

from novel_llm import gateway_pb2
from novel_llm.gateway import GatewayProvider
from novel_llm.provider import Class


async def test_schema_fallback_preserves_gateway_routing_and_priority():
    provider = GatewayProvider(address="localhost:1", tenant="novel", provider="ollama",
                               model="model", backend="local_gpu", embed_model="embed")
    schema = {"type": "object", "required": ["facts"]}

    async def complete(request, *, timeout):
        assert json.dumps(schema) in request.system
        assert request.json_mode
        assert request.no_fallback
        assert request.priority == gateway_pb2.BATCH
        assert request.tenant == "novel" and request.model == "requested"
        yield gateway_pb2.CompletionChunk(kind=gateway_pb2.TEXT, text='{"facts":[]}',
                                         served_provider="ollama", served_model="requested")

    provider._client = SimpleNamespace(Complete=complete)
    try:
        result = await provider.complete("source", json_schema=schema, cls=Class.BATCH,
                                         pin_model=True, model="requested")
        assert result.text == '{"facts":[]}'
    finally:
        await provider.aclose()
