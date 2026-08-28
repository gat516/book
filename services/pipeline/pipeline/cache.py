"""The LLM-result cache (instructions.md §6.1, §3.5; PLAN.md 1.5).

The project's biggest cost lever, and about thirty lines. Re-processing a novel,
replaying after a crash, or re-running a stage you didn't change costs zero LLM spend
because identical ``(stage, content, prompt_version, stage_config_version, model_id)``
tuples are served from here. That tuple *is* the §3.5 idempotency key, so this module
takes a key and stores a string against it — it never recomputes what a key means.

Two levels, doing different jobs (§6.1's "Redis → Postgres job"):

- **Redis** holds the raw model output. It is the fast path and it is disposable —
  eviction costs money, never correctness.
- **The Postgres ``job`` row** records that a key's work was already *written to the
  graph*. That is the durable level, and it is the one that makes an append-only sink
  idempotent: facts are INSERTed, never upserted, so re-running a chapter whose job is
  already ``done`` would duplicate every fact it ever produced. A Redis hit saves an
  LLM call; a ``done`` job row skips the stage entirely. Those are not the same check
  and collapsing them into one would be a data bug, not a performance regression.

**The write refuses to cache what it cannot attribute.** ``put()`` takes the requested
model id and the served one and drops the write when they differ (§12, §14.3). A gateway
(§14) may transparently fail over; caching model B's output under model A's key is
exactly the silent corruption ``served_model`` exists to prevent, arriving through a path
the original design never anticipated. Declining to cache is the deliberately boring fix
— no dual-keying, just "don't store what you can't name."
"""

from __future__ import annotations

import logging

from pipeline.jobs import cacheable_result

log = logging.getLogger(__name__)

KEY_PREFIX = "llm:result:"

# 14 days. Long enough that a re-run of a whole novel days later is still free, short
# enough that abandoned experiments expire. Correctness never depends on this number:
# a miss re-runs the call, and the job row (not Redis) is what prevents double-writes.
DEFAULT_TTL_S = 14 * 24 * 3600


class LLMCache:
    """Redis-backed result cache keyed by the §3.5 idempotency key."""

    def __init__(self, redis, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    async def get(self, key: str) -> str | None:
        return await self.redis.get(KEY_PREFIX + key)

    async def delete(self, key: str) -> None:
        """Discard an unusable response without touching the durable job ledger."""
        await self.redis.delete(KEY_PREFIX + key)

    async def put(
        self,
        key: str,
        value: str,
        *,
        requested_model_id: str,
        served_provider: str,
        served_model: str,
        stage: str,
    ) -> bool:
        """Store ``value`` under ``key``. Returns whether it was actually written.

        A ``False`` return is not an error — it means the response came from a model
        other than the one the key names, so there is no key under which storing it
        would be truthful.
        """
        if not cacheable_result(
            stage,
            requested_model_id=requested_model_id,
            served_provider=served_provider,
            served_model=served_model,
        ):
            log.warning(
                "not caching %s result: requested %s but %s:%s served it (§12, §14.3)",
                stage,
                requested_model_id,
                served_provider,
                served_model,
            )
            return False
        await self.redis.set(KEY_PREFIX + key, value, ex=self.ttl_s)
        return True
