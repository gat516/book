from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from pipeline import textproc_pb2, textproc_pb2_grpc
from pipeline.mentions import Alias, MentionScanRequest
from pipeline.stages.scan import ScanStage
from pipeline.textproc import GrpcTextProcClient, textproc_from_config

ADDRESS = os.getenv("TEXTPROC_TEST_ADDR")
GOLDEN_CASES = Path(__file__).parents[2] / "textproc" / "tests" / "scan_cases.json"

pytestmark = pytest.mark.skipif(not ADDRESS, reason="TEXTPROC_TEST_ADDR is not set")


class _AliasRows:
    async def fetchall(self):
        return [("hero", "Li Xiaoyao"), ("sect", "青云宗")]


class _AliasDB:
    async def execute(self, query, params):
        assert "FROM alias" in query
        assert params == ("novel", 7)
        return _AliasRows()


async def _run_scan_stage(client):
    state = SimpleNamespace(
        envelope=SimpleNamespace(
            raw_text="Li Xiaoyao抵达青云宗。",
            chapter_index=7,
        ),
        mentions=[],
    )
    context = SimpleNamespace(
        db=_AliasDB(),
        novel=SimpleNamespace(id="novel", source_lang="zh"),
        textproc=client,
    )
    await ScanStage().run(context, state)
    return [span.model_dump() for span in state.mentions]


@pytest.mark.asyncio
async def test_live_rust_service_contract() -> None:
    assert ADDRESS is not None
    channel = grpc.aio.insecure_channel(ADDRESS)
    client = GrpcTextProcClient(ADDRESS, 5)
    stub = textproc_pb2_grpc.TextProcStub(channel)
    try:
        health = health_pb2_grpc.HealthStub(channel)
        status = await health.Check(health_pb2.HealthCheckRequest(service=""), timeout=5)
        assert status.status == health_pb2.HealthCheckResponse.SERVING
        service_status = await health.Check(
            health_pb2.HealthCheckRequest(service="textproc.TextProc"), timeout=5
        )
        assert service_status.status == health_pb2.HealthCheckResponse.SERVING

        for case in json.loads(GOLDEN_CASES.read_text()):
            response = await client.scan(
                MentionScanRequest(
                    text=case["text"],
                    aliases=[Alias(alias_id=alias_id, surface=surface) for alias_id, surface in case["aliases"]],
                    lang=case["lang"],
                )
            )
            actual = [[span.alias_id, span.byte_start, span.byte_end, span.char_start, span.char_end] for span in response.spans]
            assert actual == case["spans"], case["name"]

        hashed = await stub.HashContent(textproc_pb2.HashRequest(text="abc"), timeout=5)
        assert hashed.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert hashed.near_dup_sig.startswith("simhash64-v1:")

        with pytest.raises(grpc.aio.AioRpcError) as oversized:
            await stub.HashContent(textproc_pb2.HashRequest(text="x" * (2 * 1024 * 1024 + 1)), timeout=5)
        assert oversized.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

        with pytest.raises(grpc.aio.AioRpcError) as scan_text_limit:
            await stub.ScanMentions(
                textproc_pb2.MentionScanRequest(text="x" * (2 * 1024 * 1024 + 1)),
                timeout=5,
            )
        assert scan_text_limit.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

        with pytest.raises(grpc.aio.AioRpcError) as alias_limit:
            await stub.ScanMentions(
                textproc_pb2.MentionScanRequest(
                    text="x",
                    aliases=[textproc_pb2.Alias(alias_id="e", surface="x")] * 100_001,
                ),
                timeout=5,
            )
        assert alias_limit.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

        grpc_stage = textproc_from_config("grpc", ADDRESS, 5)
        python_stage = textproc_from_config("python", "unused", 5)
        try:
            assert await _run_scan_stage(grpc_stage) == await _run_scan_stage(python_stage)
        finally:
            await grpc_stage.aclose()
            await python_stage.aclose()

        with pytest.raises(grpc.aio.AioRpcError) as retro:
            await stub.ApplyRetroUpdate(textproc_pb2.RetroRequest(text="x"), timeout=5)
        assert retro.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        await client.aclose()
        await channel.close()
