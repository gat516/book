from __future__ import annotations

import grpc
import pytest

from pipeline import textproc_pb2, textproc_pb2_grpc
from pipeline.mentions import Alias, MentionScanRequest
from pipeline.textproc import GrpcTextProcClient, PythonTextProcClient, textproc_from_config


class Servicer(textproc_pb2_grpc.TextProcServicer):
    async def ScanMentions(self, request, context):  # noqa: N802 - protobuf method name
        assert request.text == "青云宗"
        return textproc_pb2.MentionScanResponse(
            spans=[textproc_pb2.Span(alias_id="e1", byte_start=0, byte_end=9, char_start=0, char_end=3)]
        )


@pytest.mark.asyncio
async def test_grpc_client_maps_proto_spans() -> None:
    server = grpc.aio.server()
    textproc_pb2_grpc.add_TextProcServicer_to_server(Servicer(), server)
    try:
        port = server.add_insecure_port("127.0.0.1:0")
    except RuntimeError as exc:
        if "Failed to bind" in str(exc):
            pytest.skip("sandbox does not permit loopback listeners")
        raise
    await server.start()
    client = GrpcTextProcClient(f"127.0.0.1:{port}", 1)
    try:
        response = await client.scan(MentionScanRequest(text="青云宗", aliases=[Alias(alias_id="e1", surface="青云宗")], lang="zh"))
        assert response.spans[0].model_dump() == {
            "alias_id": "e1", "byte_start": 0, "byte_end": 9, "char_start": 0, "char_end": 3,
        }
    finally:
        await client.aclose()
        await server.stop(None)


@pytest.mark.asyncio
async def test_grpc_failures_do_not_fall_back() -> None:
    client = GrpcTextProcClient("127.0.0.1:1", 0.01)
    try:
        with pytest.raises(grpc.aio.AioRpcError):
            await client.scan(MentionScanRequest(text="x", aliases=[Alias(alias_id="e", surface="x")]))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_python_backend_is_explicit_fallback() -> None:
    client = textproc_from_config("python", "unused", 1)
    assert isinstance(client, PythonTextProcClient)
    response = await client.scan(MentionScanRequest(text="王国", aliases=[Alias(alias_id="short", surface="王"), Alias(alias_id="long", surface="王国")]))
    assert [span.alias_id for span in response.spans] == ["long"]
