"""Optional metadata-only DeepSeek admission (spec §14.7, §15 BYOK).

The provider call stays in QiReadr. Never send prompts, keys, schemas, or error
details to the gateway. This is a semaphore client, not a token estimator.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import replace

import grpc

from novel_llm import gateway_pb2 as pb, gateway_pb2_grpc as rpc
from novel_llm.accounts import hosted
from novel_llm.provider import AdmissionRejected, Class, Completion, SequentialBatchMixin

log = logging.getLogger(__name__)
REQUEST_TIMEOUT_S = 120.0
LEASE_TIMEOUT_S = 180.0
RPC_TIMEOUT_S = 2.0


def configure_admission_logging() -> None:
    """Enable only metadata telemetry, without enabling SDK/HTTP request logs."""
    log.setLevel(logging.INFO)
    if not log.hasHandlers():
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)


def admission_settings(*, visibility_timeout: float = 300) -> tuple[str, set[str]]:
    address = os.getenv("LLM_GATEWAY_ADMISSION_ADDR", "").strip()
    if not address:
        return "", set()
    accounts = {str(uuid.UUID(value.strip())) for value in
                os.getenv("LLM_GATEWAY_ADMISSION_ACCOUNTS", "").split(",") if value.strip()}
    if not accounts:
        raise ValueError("gateway admission requires an explicit account allowlist")
    if not REQUEST_TIMEOUT_S + 2 * RPC_TIMEOUT_S < LEASE_TIMEOUT_S < visibility_timeout:
        raise ValueError("gateway requires request < 180s lease < pipeline visibility timeout")
    return address, accounts


async def with_gateway_admission(provider, conn, *, provider_id: str,
                                 visibility_timeout: float = 300):
    address, accounts = admission_settings(visibility_timeout=visibility_timeout)
    if not address or not hosted() or provider_id != "deepseek":
        return provider
    # Resolved from the same RLS session as the account credential, never from a
    # browser header or novel id. All this account's books share capacity (§15.2).
    row = await (await conn.execute("SELECT current_account()::text")).fetchone()
    account = str(row[0]) if row and row[0] else ""
    if not account:
        raise RuntimeError("gateway admission requires account scope")
    if account not in accounts:
        return provider
    return AdmissionProvider(provider, address=address, tenant=account)


def _deferred(*, unavailable: bool = False, retry_after_s: float = 5) -> AdmissionRejected:
    exc = AdmissionRejected("gateway admission unavailable" if unavailable else "gateway capacity busy",
                            retry_after_s=max(1, retry_after_s), exact_hint=True,
                            category="unreachable" if unavailable else "rate_limited")
    # No provider request happened: do not spend the durable provider retry budget.
    exc.admission_wait = True
    return exc


class AdmissionProvider(SequentialBatchMixin):
    """Reserve → direct completion → cancellation-safe settlement; fail closed.

    DeepSeek has no native batch API: the mixin must call *this* complete method,
    otherwise delegated batch_submit would bypass admission entirely.
    """

    def __init__(self, provider, *, address: str, tenant: str):
        super().__init__()
        self._provider = provider
        self._tenant = tenant
        self._channel = grpc.aio.insecure_channel(address)
        self._client = rpc.AdmissionStub(self._channel)

    def __getattr__(self, name):
        return getattr(self._provider, name)

    async def _settle(self, reservation, result: Completion | None) -> bool:
        try:
            reply = await self._client.Settle(pb.SettleRequest(
                reservation_id=reservation.reservation_id, failed=result is None,
                actual_input_tokens=result.input_tokens if result else 0,
                actual_output_tokens=result.output_tokens if result else 0,
                actual_cache_write_tokens=result.cache_write_tokens if result else 0,
                actual_cache_read_tokens=result.cache_read_tokens if result else 0,
                served_provider=result.served_provider if result else reservation.served_provider,
                served_model=result.served_model if result else reservation.served_model,
            ), timeout=RPC_TIMEOUT_S)
            settled = reply.settled and not reply.was_late
        except Exception:
            settled = False
        if not settled:
            # Do not discard a paid completion or retry it just because settlement
            # failed. The 180s lease is the bounded crash/transport recovery path.
            log.warning("gateway_admission event=settlement_failed")
        return settled

    async def _release(self, reservation, result):
        task = asyncio.create_task(self._settle(reservation, result))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return task.result()

    async def complete(self, prompt: str, *, cls: Class = Class.BATCH, **kwargs) -> Completion:
        model = kwargs.get("model") or self._provider._model
        request = pb.ReserveRequest(tenant=self._tenant, provider="deepseek", model=model,
                                   backend=pb.HOSTED,
                                   priority=pb.INTERACTIVE if cls is Class.INTERACTIVE else pb.BATCH,
                                   no_fallback=True, idempotency_key=uuid.uuid4().hex)
        started = time.monotonic()
        # Shield acquisition so cancellation cannot lose a known reservation ID.
        pending = asyncio.ensure_future(self._client.Reserve(request, timeout=RPC_TIMEOUT_S))
        try:
            reservation = await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                reservation = await pending
            except Exception:
                raise asyncio.CancelledError from None
            if reservation.allowed and reservation.reservation_id:
                await self._release(reservation, None)
            raise
        except grpc.RpcError:
            log.warning("gateway_admission event=unavailable class=%s", cls.value)
            raise _deferred(unavailable=True) from None
        reserve_s = time.monotonic() - started
        if not reservation.allowed:
            log.info("gateway_admission event=rejected class=%s reserve_s=%.6f", cls.value, reserve_s)
            raise _deferred(retry_after_s=reservation.retry_after_ms / 1000)
        if not reservation.reservation_id:
            raise _deferred(unavailable=True)
        result = None
        called_at = time.monotonic()
        try:
            if (reservation.served_provider, reservation.served_model) != ("deepseek", model):
                raise _deferred(unavailable=True)
            # Total wall-clock bound, including SDK work, not only HTTP read timeout.
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                result = await self._provider.complete(prompt, cls=cls, **kwargs)
        except TimeoutError:
            # Unlike a denied reservation, this *did* reach the provider and must
            # count toward bounded provider recovery rather than infinite deferral.
            raise AdmissionRejected("provider request timeout", retry_after_s=5,
                                    category="unreachable") from None
        finally:
            provider_s = time.monotonic() - called_at
            settle_at = time.monotonic()
            settled = await self._release(reservation, result)
            settle_s = time.monotonic() - settle_at
            log.info("gateway_admission event=completed class=%s success=%s settled=%s "
                     "reserve_s=%.6f provider_s=%.6f settle_s=%.6f", cls.value,
                     result is not None, settled, reserve_s, provider_s, settle_s)
        return replace(result, timings={**result.timings, "gateway_reserve_s": reserve_s,
                                       "gateway_settle_s": settle_s})

    async def aclose(self):
        await self._channel.close()
        await self._provider.aclose()
