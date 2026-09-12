import asyncio

import pytest

from pipeline.inference_runtime import (
    CooldownProvider, HostedCooldown, ResourceClosingProvider, coordinated_provider,
    credential_fingerprint, effective_schema_transport, runtime_identity,
)


class Redis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    async def set(self, key, value, *, px, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expiry[key] = asyncio.get_running_loop().time() + px / 1000
        return True

    async def pttl(self, key):
        if key not in self.values:
            return -2
        left = self.expiry[key] - asyncio.get_running_loop().time()
        if left <= 0:
            self.values.pop(key, None)
            return -2
        return int(left * 1000)


def test_runtime_identity_changes_with_output_and_schema_transport():
    assert runtime_identity(output_tokens=100, schema_transport="native") != runtime_identity(
        output_tokens=200, schema_transport="native")
    assert runtime_identity(output_tokens=100, schema_transport="native") != runtime_identity(
        output_tokens=100, schema_transport="duplicated")
    assert credential_fingerprint("groq", "https://api.example/v1/", "secret") == credential_fingerprint(
        "groq", "https://api.example/v1", "secret")
    assert "secret" not in credential_fingerprint("groq", "https://api.example/v1", "secret")
    assert "credential" not in vars(HostedCooldown(None, provider="groq", base_url="https://api.example", credential="secret"))
    assert runtime_identity(output_tokens=100, context_tokens=10) != runtime_identity(output_tokens=100, context_tokens=20)
    assert runtime_identity(output_tokens=100, request_tokens=1000) != runtime_identity(
        output_tokens=100, request_tokens=2000)


def test_runtime_identity_rejects_request_budget_without_output_headroom():
    with pytest.raises(ValueError, match="request_tokens must exceed output_tokens"):
        runtime_identity(output_tokens=100, request_tokens=100)


def test_schema_transport_uses_provider_capability_without_provider_branches():
    class Native:
        def schema_transport(self, model):
            assert model == "one"
            return "native"

    class Prompt:
        def schema_transport(self, model):
            assert model == "two"
            return "prompt"

    assert effective_schema_transport(Native(), "one") == "native"
    assert effective_schema_transport(Prompt(), "two") == "prompt"


async def test_cooldown_is_shared_and_cancellation_interrupts_wait():
    redis = Redis()
    first = HostedCooldown(redis, provider="groq", base_url="https://api.example", credential="secret",
                            poll_seconds=.01)
    second = HostedCooldown(redis, provider="groq", base_url="https://api.example", credential="secret",
                            poll_seconds=.01)
    await first.note(.05)
    with pytest.raises(asyncio.CancelledError):
        task = asyncio.create_task(second.wait())
        await asyncio.sleep(.01)
        task.cancel()
        await task
    waited = await second.wait()
    assert waited >= .03
    assert first._key == second._key


async def test_longer_cooldown_is_not_replaced_by_shorter_one():
    redis = Redis()
    cooldown = HostedCooldown(redis, provider="x", base_url="https://x", credential="k")
    await cooldown.note(.2)
    await cooldown.note(.01)
    assert await cooldown._pttl() > 100


async def test_coordinated_provider_wraps_hosted_calls_and_preserves_cancellation():
    class Provider:
        _api_key = "secret"
        _base_url = "https://api.example"

        async def complete(self, **kwargs):
            return "ok"

        async def batch_submit(self, requests):
            return "batch"

        async def batch_poll(self, batch_id):
            return []

        async def embed(self, texts, **kwargs):
            return []

        async def aclose(self):
            return None

    redis = Redis()
    wrapped = coordinated_provider(Provider(), redis, provider_id="groq")
    assert isinstance(wrapped, CooldownProvider)
    assert await wrapped.batch_submit([]) == "batch"
    assert await wrapped.batch_poll("batch") == []


async def test_owned_redis_is_closed_when_credential_is_unavailable():
    class Provider:
        async def aclose(self):
            self.closed = True

    class OwnedRedis:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    redis = OwnedRedis()
    provider = Provider()
    wrapped = coordinated_provider(provider, redis, provider_id="groq", owns_redis=True)
    assert isinstance(wrapped, ResourceClosingProvider)
    await wrapped.aclose()
    assert provider.closed and redis.closed


async def test_long_shared_cooldown_rejects_instead_of_holding_the_caller():
    from pipeline.llm.provider import AdmissionRejected

    class Provider:
        _api_key = "secret"
        _base_url = "https://api.example"
        calls = 0

        async def complete(self, **kwargs):
            Provider.calls += 1
            return "ok"

    redis = Redis()
    redis.get = lambda key: asyncio.sleep(0, result=redis.values.get(key))
    wrapped = coordinated_provider(Provider(), redis, provider_id="groq")
    await wrapped._cooldown.note(1560, "quota_exhausted")
    with pytest.raises(AdmissionRejected) as rejected:
        await asyncio.wait_for(wrapped.complete(), 1)
    assert rejected.value.shared_cooldown is True
    assert rejected.value.category == "quota_exhausted"
    assert 1500 < rejected.value.retry_after_s <= 1560
    assert Provider.calls == 0


async def test_short_shared_cooldown_is_still_waited_inline():
    class Provider:
        async def complete(self, **kwargs):
            return "ok"

    cooldown = HostedCooldown(Redis(), provider="groq", base_url="https://x", credential="k",
                              poll_seconds=.01)
    wrapped = CooldownProvider(Provider(), cooldown, max_inline_wait_s=1)
    await cooldown.note(.05)
    assert await asyncio.wait_for(wrapped.complete(), 1) == "ok"
