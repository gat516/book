"""The LLM-result cache (instructions.md §6.1, §12, §14.3; PLAN.md 1.5).

The interesting assertion is not that a cache caches. It is that the cache **refuses to
store output it cannot attribute**: if the response came back served by a model other
than the one the key names, there is no key under which storing it would be truthful.
That is the §12 silent-corruption path, and today no backend can even trigger it — a
direct provider always echoes the model it was asked for. The test exists so that the
day a gateway (§14) starts failing over, the behavior is already pinned rather than
discovered from a translation that changed voice mid-novel.

Pure — a dict-backed stand-in for Redis, no network.
"""

from __future__ import annotations

import pytest

from fixtures import FakeRedis

from pipeline.cache import KEY_PREFIX, LLMCache

KEY = "a" * 64
REQUESTED = "ollama:qwen2.5:14b"


@pytest.fixture
def cache() -> tuple[LLMCache, FakeRedis]:
    redis = FakeRedis()
    return LLMCache(redis, ttl_s=60), redis


async def _put(cache: LLMCache, value: str, *, served: str = REQUESTED) -> bool:
    provider, _, model = served.partition(":")
    return await cache.put(
        KEY,
        value,
        requested_model_id=REQUESTED,
        served_provider=provider,
        served_model=model,
        stage="state",
    )


async def test_roundtrip(cache):
    llm_cache, _ = cache
    assert await llm_cache.get(KEY) is None
    assert await _put(llm_cache, '{"facts": []}') is True
    assert await llm_cache.get(KEY) == '{"facts": []}'


async def test_key_is_namespaced(cache):
    """book's Redis also holds the job queue (§15.5). An unprefixed key would collide
    with a queue name eventually, and the failure would look like queue corruption."""
    llm_cache, redis = cache
    await _put(llm_cache, "x")
    assert list(redis.store) == [KEY_PREFIX + KEY]


async def test_ttl_is_applied(cache):
    llm_cache, redis = cache
    await _put(llm_cache, "x")
    assert redis.ttls[KEY_PREFIX + KEY] == 60


async def test_refuses_to_cache_a_different_served_model(cache):
    """§12 risk #4 / §14.3. A failover served this; caching it under the requested
    model's key would make every later hit serve model B's output as if model A produced
    it. Declining is the whole mechanism — assert both the refusal and that nothing
    landed."""
    llm_cache, redis = cache
    assert await _put(llm_cache, "from another model", served="ollama:llama3.1:8b") is False
    assert redis.store == {}
    assert await llm_cache.get(KEY) is None


async def test_refuses_on_provider_switch_even_with_same_model_name(cache):
    """The provider half of model_id matters too: two backends can serve a model of the
    same name and mean different weights."""
    llm_cache, redis = cache
    assert await _put(llm_cache, "x", served="anthropic:qwen2.5:14b") is False
    assert redis.store == {}


async def test_a_refusal_does_not_evict_an_existing_entry(cache):
    """A good entry followed by a failover response must survive. Treating the refusal
    as a delete would turn a cost optimization into a cost regression under exactly the
    conditions (contention, failover) where spend is already highest."""
    llm_cache, _ = cache
    await _put(llm_cache, "good")
    await _put(llm_cache, "failover", served="ollama:llama3.1:8b")
    assert await llm_cache.get(KEY) == "good"
