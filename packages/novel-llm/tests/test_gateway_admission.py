import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest

from novel_llm import gateway_pb2 as pb
from novel_llm.gateway_admission import AdmissionProvider, admission_settings, with_gateway_admission
from novel_llm.provider import AdmissionRejected, Class, Completion

ACCOUNT = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
async def wrapped():
    direct = SimpleNamespace(_model="deepseek-v4-flash", _api_key="private-key",
                             complete=AsyncMock(return_value=Completion(
                                 "private answer", "deepseek", "deepseek-v4-flash",
                                 input_tokens=12, output_tokens=3, cache_read_tokens=5,
                                 cache_write_tokens=2, timings={"existing": 1})),
                             aclose=AsyncMock())
    provider = AdmissionProvider(direct, address="localhost:1", tenant=ACCOUNT)
    provider._client = SimpleNamespace(
        Reserve=AsyncMock(return_value=pb.ReserveReply(allowed=True, reservation_id="reservation",
                         served_provider="deepseek", served_model="deepseek-v4-flash")),
        Settle=AsyncMock(return_value=pb.SettleReply(settled=True)))
    yield provider
    await provider.aclose()


async def test_metadata_only_reserve_preserves_provider_options_and_usage(wrapped):
    options = dict(system="private system", json_schema={"private": "schema"},
                   json_mode=True, pin_model=True, reasoning_effort="high", max_output_tokens=44)
    result = await wrapped.complete("private prompt", cls=Class.INTERACTIVE, **options)
    request = wrapped._client.Reserve.call_args.args[0]
    assert request.tenant == ACCOUNT and request.provider == "deepseek"
    assert request.priority == pb.INTERACTIVE and request.backend == pb.HOSTED and request.no_fallback
    assert "private" not in str(request)
    wrapped._provider.complete.assert_awaited_once_with("private prompt", cls=Class.INTERACTIVE, **options)
    settle = wrapped._client.Settle.call_args.args[0]
    assert not settle.failed and settle.actual_input_tokens == 12 and settle.actual_output_tokens == 3
    assert settle.actual_cache_read_tokens == 5 and settle.actual_cache_write_tokens == 2
    assert result.text == "private answer" and result.timings["existing"] == 1
    assert result.timings["gateway_reserve_s"] >= 0


@pytest.mark.parametrize("mode", ["denied", "unavailable", "wrong_model", "missing_id"])
async def test_rejection_never_calls_provider_or_spends_retry_budget(wrapped, mode):
    if mode == "denied":
        wrapped._client.Reserve.return_value = pb.ReserveReply(retry_after_ms=7000)
    elif mode == "unavailable":
        wrapped._client.Reserve.side_effect = grpc.RpcError("private transport detail")
    elif mode == "wrong_model":
        wrapped._client.Reserve.return_value.served_model = "other"
    else:
        wrapped._client.Reserve.return_value.reservation_id = ""
    with pytest.raises(AdmissionRejected) as caught:
        await wrapped.complete("private")
    assert caught.value.admission_wait
    assert "private" not in str(caught.value)
    wrapped._provider.complete.assert_not_awaited()
    assert wrapped._client.Settle.await_count == (1 if mode == "wrong_model" else 0)


async def test_provider_failure_releases_and_preserves_error(wrapped):
    error = ValueError("provider error")
    wrapped._provider.complete.side_effect = error
    with pytest.raises(ValueError) as caught:
        await wrapped.complete("private")
    assert caught.value is error
    assert wrapped._client.Settle.call_args.args[0].failed


async def test_provider_timeout_releases(wrapped, monkeypatch):
    monkeypatch.setattr("novel_llm.gateway_admission.REQUEST_TIMEOUT_S", .01)
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()
    wrapped._provider.complete.side_effect = hang
    with pytest.raises(AdmissionRejected) as caught:
        await wrapped.complete("private")
    assert not getattr(caught.value, "admission_wait", False)
    assert caught.value.category == "unreachable"
    assert wrapped._client.Settle.call_args.args[0].failed


@pytest.mark.parametrize("phase", ["reserve", "provider", "settle"])
async def test_cancellation_releases_reservation(wrapped, phase):
    entered, finish = asyncio.Event(), asyncio.Event()
    target = {"reserve": wrapped._client.Reserve, "provider": wrapped._provider.complete,
              "settle": wrapped._client.Settle}[phase]
    async def wait(*args, **kwargs):
        entered.set()
        await finish.wait()
        return target.return_value
    target.side_effect = wait
    task = asyncio.create_task(wrapped.complete("private"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wrapped._client.Settle.await_count == 1
    if phase == "reserve":
        wrapped._provider.complete.assert_not_awaited()


async def test_settle_failure_keeps_paid_result_and_logs_no_content(wrapped, caplog):
    wrapped._client.Settle.side_effect = grpc.RpcError("private key and response")
    assert (await wrapped.complete("private")).text == "private answer"
    assert "settlement_failed" in caplog.text and "private" not in caplog.text


async def test_sequential_batches_cannot_bypass_admission(wrapped):
    batch = await wrapped.batch_submit([{"id": str(i), "prompt": "private", "system": ""} for i in range(3)])
    assert len(await wrapped.batch_poll(batch)) == 3
    assert wrapped._client.Reserve.await_count == wrapped._client.Settle.await_count == 3
    assert all(call.args[0].priority == pb.BATCH for call in wrapped._client.Reserve.call_args_list)


def test_settings_off_and_explicit_allowlist(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_ADMISSION_ADDR", raising=False)
    assert admission_settings(visibility_timeout=1) == ("", set())
    monkeypatch.setenv("LLM_GATEWAY_ADMISSION_ADDR", "gateway:8081")
    monkeypatch.delenv("LLM_GATEWAY_ADMISSION_ACCOUNTS", raising=False)
    with pytest.raises(ValueError, match="allowlist"):
        admission_settings()
    monkeypatch.setenv("LLM_GATEWAY_ADMISSION_ACCOUNTS", ACCOUNT)
    assert admission_settings() == ("gateway:8081", {ACCOUNT})
    with pytest.raises(ValueError, match="visibility"):
        admission_settings(visibility_timeout=180)


async def test_factory_account_scope_off_switch_and_other_providers(monkeypatch):
    monkeypatch.setenv("BOOK_MODE", "hosted")
    monkeypatch.setenv("LLM_GATEWAY_ADMISSION_ADDR", "gateway:8081")
    monkeypatch.setenv("LLM_GATEWAY_ADMISSION_ACCOUNTS", ACCOUNT)
    direct = SimpleNamespace(aclose=AsyncMock())
    conn = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(ACCOUNT,)))))
    assert await with_gateway_admission(direct, conn, provider_id="gemini") is direct
    conn.execute.assert_not_awaited()
    provider = await with_gateway_admission(direct, conn, provider_id="deepseek")
    assert isinstance(provider, AdmissionProvider) and provider._tenant == ACCOUNT
    await provider.aclose()
    conn.execute.return_value.fetchone.return_value = ("00000000-0000-4000-8000-000000000002",)
    assert await with_gateway_admission(direct, conn, provider_id="deepseek") is direct
    monkeypatch.delenv("LLM_GATEWAY_ADMISSION_ADDR")
    assert await with_gateway_admission(direct, conn, provider_id="deepseek") is direct
