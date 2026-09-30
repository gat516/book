"""Opt-in real Go gateway/Redis contract test; synthetic provider, zero paid calls.

GATEWAY_TEST_BINARY=/path/gateway GATEWAY_TEST_REDIS_ADDR=127.0.0.1:16379 pytest ... -s
Redis must be disposable; this test uses a unique prefix and never flushes it.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import uuid

import grpc
import pytest
import yaml

from novel_llm import gateway_pb2 as pb, gateway_pb2_grpc as rpc
from novel_llm.gateway_admission import AdmissionProvider
from novel_llm.provider import AdmissionRejected, Class, Completion


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.asyncio
async def test_real_gateway_shared_capacity_lifecycle_and_outage(tmp_path):
    binary, redis = os.getenv("GATEWAY_TEST_BINARY"), os.getenv("GATEWAY_TEST_REDIS_ADDR")
    if not binary or not redis:
        pytest.skip("opt-in gateway binary and disposable Redis required")
    spec = importlib.util.spec_from_file_location("gateway_trial", Path(__file__).parents[2] / "deploy/hosted/gateway_trial.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tenant = str(uuid.uuid4())
    resources, _ = module.render("registry/gateway:test-123", "registry/python:test-123", tenant)
    config = yaml.safe_load(next(r for r in resources if r["kind"] == "ConfigMap")["data"]["config.yaml"])
    address = f"127.0.0.1:{free_port()}"
    config["server"].update(grpc_addr=address, metrics_addr=f"127.0.0.1:{free_port()}")
    config["redis"].update(addr=redis, key_prefix="book-integration-" + uuid.uuid4().hex)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    process = subprocess.Popen([binary, "-config", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    channel = grpc.aio.insecure_channel(address)
    entered, release = asyncio.Queue(), asyncio.Event()
    calls = active = peak = 0

    class Direct:
        _model = "deepseek-v4-flash"
        async def complete(self, *args, **kwargs):
            nonlocal calls, active, peak
            calls += 1
            active += 1
            peak = max(peak, active)
            entered.put_nowait(True)
            try:
                await release.wait()
                return Completion("synthetic", "deepseek", self._model, 10, 2)
            finally:
                active -= 1
        async def aclose(self): pass

    workers = [AdmissionProvider(Direct(), address=address, tenant=tenant) for _ in range(3)]
    tasks = []
    try:
        await asyncio.wait_for(channel.channel_ready(), 10)
        client = rpc.AdmissionStub(channel)
        status_request = pb.StatusRequest(tenant=tenant, provider="deepseek", model="deepseek-v4-flash")
        for index, priority in enumerate((Class.BATCH, Class.INTERACTIVE)):
            tasks.append(asyncio.create_task(workers[index].complete("synthetic", cls=priority)))
            await asyncio.wait_for(entered.get(), 5)
        state = await client.GetStatus(status_request, timeout=2)
        assert state.pending_reservations == 2 and state.rpm_remaining == 0
        with pytest.raises(AdmissionRejected):
            await workers[2].complete("must not reach provider")
        assert calls == peak == 2
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert (await client.GetStatus(status_request, timeout=2)).pending_reservations == 1
        release.set()
        await tasks[1]
        samples = []
        for index in range(40):
            result = await workers[index % 3].complete("synthetic")
            samples.append(result.timings["gateway_reserve_s"] + result.timings["gateway_settle_s"])
        assert (await client.GetStatus(status_request, timeout=2)).pending_reservations == 0
        before_outage = calls
        process.terminate()
        await asyncio.to_thread(process.wait, 10)
        with pytest.raises(AdmissionRejected) as error:
            await workers[2].complete("must not bypass failed gateway")
        assert error.value.admission_wait and calls == before_outage
        print(json.dumps({"test": "local-synthetic-admission", "paid_requests": 0,
                          "capacity": 2, "peak_provider_concurrency": peak,
                          "over_capacity_calls_blocked": 1, "outage_calls_blocked": 1,
                          "pending_after_settlement": 0, "overhead_samples": len(samples),
                          "reserve_plus_settle_median_ms": round(statistics.median(samples)*1000, 3),
                          "reserve_plus_settle_max_ms": round(max(samples)*1000, 3)}))
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for worker in workers: await worker.aclose()
        await channel.close()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
