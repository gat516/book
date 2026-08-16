"""Reusable fixtures: DB seeding plus the in-memory stand-ins for Redis and an LLM
backend. Not production code — importable helpers, not pytest fixtures themselves (those
live in conftest.py / the test files)."""

from __future__ import annotations

import uuid

from pipeline.config import Config
from pipeline.llm.provider import Class, Completion


def make_config(**overrides) -> Config:
    """A Config with every field set to a harmless local default. Tests override only
    the field under test, so adding a Config field doesn't touch every test file."""
    base = dict(
        database_url="postgres://x",
        redis_url="redis://x",
        object_endpoint="localhost:9000",
        object_access_key="k",
        object_secret_key="s",
        object_bucket="raw-chapters",
        object_secure=False,
        llm_provider="ollama",
        llm_model_translate="qwen2.5:14b",
        llm_model_extract="qwen2.5:14b",
        embed_model="nomic-embed-text",
        embed_dim=768,
        ollama_host="http://localhost:11434",
        prompt_version="1",
        config_version="1",
        queue_timeout=5,
        visibility_timeout=300,
        reaper_interval=5,
    )
    base.update(overrides)
    return Config(**base)


class FakeRedis:
    """Only the calls LLMCache makes. Records TTLs so expiry is assertable."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttls[key] = ex


class FakeProvider:
    """An LLMProvider that returns canned text and counts calls.

    Defaults to echoing back the model it was asked for, which is what a direct backend
    does (§5.4) and what keeps results cacheable. ``served_model`` can be overridden to
    simulate a gateway failover (§14.3).
    """

    def __init__(
        self,
        response: str = "{}",
        *,
        provider: str = "ollama",
        served_model: str | None = None,
        embed_dim: int = 768,
    ) -> None:
        self.response = response
        self.provider = provider
        self.served_model = served_model
        self.embed_dim = embed_dim
        self.calls: list[dict] = []

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        cls: Class = Class.BATCH,
        pin_model: bool = False,
        model: str | None = None,
    ) -> Completion:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "json_mode": json_mode,
                "cls": cls,
                "pin_model": pin_model,
                "model": model,
            }
        )
        return Completion(
            text=self.response,
            served_provider=self.provider,
            served_model=self.served_model or (model or ""),
        )

    async def embed(self, texts: list[str], *, cls: Class = Class.BATCH) -> list[list[float]]:
        return [[0.0] * self.embed_dim for _ in texts]


async def make_novel(
    conn,
    *,
    source_lang: str = "zh",
    target_lang: str = "en",
    ontology: str = "{}",
) -> str:
    novel_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO novel (id, title, source_lang, target_lang, ontology) "
        "VALUES (%s, %s, %s, %s, %s)",
        (novel_id, "Test Novel", source_lang, target_lang, ontology),
    )
    return novel_id


async def delete_novel(conn, novel_id: str) -> None:
    """Manual cascade — 0001 declares FKs without ON DELETE CASCADE. ``job`` has no FK
    at all (0001), so it is cleaned by novel_id like the rest rather than by cascade."""
    for table in ("fact", "edge", "event", "chunk", "alias", "glossary", "entity", "job", "chapter", "novel"):
        if table == "alias":
            await conn.execute(
                "DELETE FROM alias WHERE entity_id IN (SELECT id FROM entity WHERE novel_id = %s)",
                (novel_id,),
            )
        elif table == "novel":
            await conn.execute("DELETE FROM novel WHERE id = %s", (novel_id,))
        else:
            await conn.execute(f"DELETE FROM {table} WHERE novel_id = %s", (novel_id,))


async def seed_flashback(conn, novel_id: str) -> dict:
    """The Phase 2 tripwire fixture (PLAN.md 1.4 Task 6): an entity revealed at
    story-time chapter 10 but whose fact isn't extracted/knowable until chapter 500 —
    a flashback. A reader at chapter 220 must not see it; the gate gates on
    source_chapter (knowledge-time), never valid_from_chapter (story-time)."""
    entity_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, %s, %s, %s)",
        (entity_id, novel_id, "character", "Flashback Character", 10),
    )
    row = await (
        await conn.execute(
            "INSERT INTO fact (novel_id, entity_id, attribute, value, "
            "valid_from_chapter, source_chapter) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (novel_id, entity_id, "secret_origin", "was the villain all along", 10, 500),
        )
    ).fetchone()
    return {"entity_id": entity_id, "fact_id": row[0]}
