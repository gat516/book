"""STATE EXTRACT end-to-end into the graph (PLAN.md 1.5; instructions.md §5 step 5, §6.1).

The three properties this phase is actually buying, in order of how expensive they are
to get wrong:

1. **Re-running a chapter does not duplicate its knowledge.** ``fact``/``edge``/``event``
   are append-only (§0.2) — INSERT, never upsert — so "just run it again" is a data
   corruption path unless something upstream declines to run. That something is the
   ``job`` row, and it is checked before the cache, not instead of it.
2. **A crash between the LLM call and the write costs nothing to redo.** The Redis
   result cache covers the gap: the job never reached ``done``, so the work re-runs, but
   it re-runs without paying for the model again.
3. **Knowledge-time is set by the pipeline, story-time by the extractor.** ``source_chapter``
   is the authorization key (§0.3) and is never taken from model output.

Needs a live Postgres — skipped cleanly via db_conn (conftest.py) when unreachable. The
LLM and Redis are fakes; nothing here talks to a network.
"""

from __future__ import annotations

import json

import pytest
from fixtures import (
    FakeProvider,
    FakeRedis,
    delete_novel,
    make_config,
    make_novel,
    seed_entities,
)

from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.jobs import idempotency_key
from pipeline.stages.graph_write import GraphWriteStage
from pipeline.stages.state import StateStage, response_cache_key

pytestmark = pytest.mark.db

CHAPTER = 41
RAW_HASH = "sha256:deadbeef"

ONTOLOGY = {
    "kinds": ["character", "sect"],
    "attributes": [{"name": "rank", "kinds": ["character"]}],
    "relations": ["member_of"],
}

RESPONSE = json.dumps(
    {
        "entities": [
            {"surface": "Li Xiaoyao", "kind": "character"},
            {"surface": "Azure Cloud Sect", "kind": "sect"},
        ],
        "facts": [
            {
                "entity": "Li Xiaoyao",
                "attribute": "rank",
                "value": "Foundation Establishment",
                "confidence": 0.9,
            }
        ],
        "edges": [
            {"src": "Li Xiaoyao", "dst": "Azure Cloud Sect", "rel_type": "member_of"}
        ],
        "events": [{"summary": "He breaks through.", "entities": ["Li Xiaoyao"]}],
    }
)


def _ctx(db, novel_id, provider, cache, *, ontology=None) -> StageContext:
    return StageContext(
        novel=NovelMeta(
            id=novel_id,
            source_lang="en",
            target_lang="en",
            ontology=ontology if ontology is not None else ONTOLOGY,
        ),
        language_profile=language_profile_for("en"),
        provider=provider,
        batch_manager=BatchManager(provider),
        embed_provider=provider,
        db=db,
        objects=None,
        cfg=make_config(),
        cache=cache,
    )


def _state() -> PipelineState:
    return PipelineState(
        envelope=ChapterEnvelope(
            novel_id="unused",
            chapter_index=CHAPTER,
            raw_text="Li Xiaoyao broke through at the Azure Cloud Sect.",
            source_lang="en",
            source_meta=SourceMeta(raw_hash=RAW_HASH),
        )
    )


async def _run(
    ctx, *, stages=(StateStage(), GraphWriteStage()), resolutions: dict[str, str] | None = None
) -> PipelineState:
    """Run stages over one chapter. ``resolutions`` stands in for RESOLVE, which these
    tests deliberately don't run — since 1.6 graph-write binds names only through that
    map and creates no entities itself, so a test that expects facts must supply it."""
    state = _state()
    state.envelope.novel_id = ctx.novel.id
    state.resolutions = dict(resolutions or {})
    for stage in stages:
        await stage.run(ctx, state)
    return state


async def _counts(db, novel_id) -> dict[str, int]:
    counts = {}
    for table in ("entity", "fact", "edge", "event"):
        row = await (
            await db.execute(f"SELECT count(*) FROM {table} WHERE novel_id = %s", (novel_id,))
        ).fetchone()
        counts[table] = row[0]
    return counts


# The surfaces RESPONSE names, with their kinds — what a successful RESOLVE would have
# produced for this chapter.
CAST = {"Li Xiaoyao": "character", "Azure Cloud Sect": "sect"}


@pytest.fixture
async def novel(db_conn):
    novel_id = await make_novel(db_conn, source_lang="en", ontology=json.dumps(ONTOLOGY))
    try:
        yield novel_id
    finally:
        await delete_novel(db_conn, novel_id)


# --- the write path ----------------------------------------------------------


async def test_extraction_lands_in_the_graph(db_conn, novel):
    provider = FakeProvider(RESPONSE)
    resolutions = await seed_entities(db_conn, novel, CAST)
    await _run(_ctx(db_conn, novel, provider, LLMCache(FakeRedis())), resolutions=resolutions)

    assert await _counts(db_conn, novel) == {"entity": 2, "fact": 1, "edge": 1, "event": 1}

    row = await (
        await db_conn.execute(
            "SELECT attribute, value, source_chapter, valid_from_chapter, confidence "
            "FROM fact WHERE novel_id = %s",
            (novel,),
        )
    ).fetchone()
    attribute, value, source_chapter, valid_from, confidence = row
    assert (attribute, value) == ("rank", "Foundation Establishment")
    # Knowledge-time is the chapter being processed, set by the pipeline (§0.3). Story
    # time defaults to it when the extractor didn't claim a flashback.
    assert source_chapter == CHAPTER
    assert valid_from == CHAPTER
    assert confidence == pytest.approx(0.9)


async def test_stage_asks_for_json_at_batch_priority(db_conn, novel):
    """Seam prep (§14.2) is only real if call sites actually use it: an offline pipeline
    stage is BATCH, and a stage whose output is parsed as JSON asks for JSON."""
    from pipeline.llm.provider import Class

    provider = FakeProvider(RESPONSE)
    await _run(_ctx(db_conn, novel, provider, LLMCache(FakeRedis())))
    call = provider.calls[0]
    assert call["json_mode"] is True
    assert call["json_schema"]["$defs"]["ExtractedFact"]["properties"]["value"]["minLength"] == 1
    assert call["cls"] is Class.BATCH
    assert len(provider.batch_requests) == 1
    assert len(provider.batch_polls) == 1
    # state-extract is structured data: a different model is a quality variance, not a
    # discontinuity, so it must NOT pin (§15.4 case 2 — only translate pins).
    assert call["pin_model"] is False


async def test_chapter_text_is_not_in_the_cacheable_prefix(db_conn, novel):
    """§6.2: the system block is what providers prefix-cache. Chapter text leaking into
    it silently multiplies the input bill with no failing test anywhere else."""
    provider = FakeProvider(RESPONSE)
    state = await _run(_ctx(db_conn, novel, provider, LLMCache(FakeRedis())))
    call = provider.calls[0]
    assert state.envelope.raw_text in call["prompt"]
    assert state.envelope.raw_text not in call["system"]
    assert "member_of" in call["system"]  # the ontology IS in the stable prefix


# --- idempotency: the expensive-to-get-wrong part ----------------------------


async def test_rerun_does_not_duplicate_or_recharge(db_conn, novel):
    """The headline assertion of 1.5. Facts are append-only, so a second run that
    re-inserted them would double every fact in the novel — and this is the exact path a
    reaper requeue (§6.3) takes after a slow chapter."""
    provider = FakeProvider(RESPONSE)
    cache = LLMCache(FakeRedis())

    await _run(_ctx(db_conn, novel, provider, cache))
    first = await _counts(db_conn, novel)

    await _run(_ctx(db_conn, novel, provider, cache))

    assert await _counts(db_conn, novel) == first
    assert len(provider.calls) == 1, "a done job must not reach the model at all"


async def test_crash_before_write_reuses_the_cached_response(db_conn, novel):
    """Property 2: the job never reached ``done`` (the write is missing), so the work
    re-runs — but the Redis entry means it re-runs for free. Running the state stage
    alone is precisely that crash: the LLM answered, nothing was written."""
    provider = FakeProvider(RESPONSE)
    cache = LLMCache(FakeRedis())
    ctx = _ctx(db_conn, novel, provider, cache)
    resolutions = await seed_entities(db_conn, novel, CAST)

    await _run(ctx, stages=(StateStage(),), resolutions=resolutions)
    assert len(provider.calls) == 1
    assert (await _counts(db_conn, novel))["fact"] == 0

    state = await _run(ctx, resolutions=resolutions)
    assert len(provider.calls) == 1, "cache hit must not re-call the model"
    assert state.extraction is not None
    assert (await _counts(db_conn, novel))["fact"] == 1


@pytest.mark.parametrize("invalid", [
    "not JSON",
    json.dumps({"facts": [{"entity": "Li Xiaoyao", "attribute": "rank", "value": []}]}),
])
async def test_invalid_response_is_not_cached_and_retry_can_recover(db_conn, novel, invalid):
    provider = FakeProvider(invalid)
    cache = LLMCache(FakeRedis())
    ctx = _ctx(db_conn, novel, provider, cache)
    key = response_cache_key(idempotency_key("state", RAW_HASH, ctx.cfg, ontology=ctx.novel.ontology))

    with pytest.raises(ValueError):
        await _run(ctx, stages=(StateStage(),))
    assert await cache.get(key) is None

    provider.response = RESPONSE
    state = await _run(ctx, stages=(StateStage(),))
    assert state.extraction is not None
    assert len(provider.calls) == 2
    assert await cache.get(key) == RESPONSE


@pytest.mark.parametrize("fresh", [RESPONSE, "still invalid"])
async def test_invalid_cache_is_evicted_before_retry(db_conn, novel, fresh):
    provider = FakeProvider(fresh)
    cache = LLMCache(FakeRedis())
    ctx = _ctx(db_conn, novel, provider, cache)
    key = response_cache_key(idempotency_key("state", RAW_HASH, ctx.cfg, ontology=ctx.novel.ontology))
    await cache.put(
        key, '{"facts": [{"entity": "Li Xiaoyao", "attribute": "rank", "value": []}]}',
        requested_model_id="ollama:qwen2.5:14b", served_provider="ollama",
        served_model="qwen2.5:14b", stage="state",
    )

    if fresh == RESPONSE:
        state = await _run(ctx, stages=(StateStage(),))
        assert state.extraction is not None
        assert await cache.get(key) == RESPONSE
    else:
        with pytest.raises(ValueError):
            await _run(ctx, stages=(StateStage(),))
        assert await cache.get(key) is None
    assert len(provider.calls) == 1


async def test_old_prompt_cache_is_bypassed_but_completed_graph_is_not_replayed(db_conn, novel):
    provider = FakeProvider(RESPONSE)
    cache = LLMCache(FakeRedis())
    ctx = _ctx(db_conn, novel, provider, cache)
    key = idempotency_key("state", RAW_HASH, ctx.cfg, ontology=ctx.novel.ontology)
    await cache.put(key, '{}', requested_model_id="ollama:qwen2.5:14b",
                    served_provider="ollama", served_model="qwen2.5:14b", stage="state")
    await _run(ctx)
    assert len(provider.calls) == 1, "unfinished work needs the new prompt/schema"
    # Simulate a cache flush or further response-version change after committing.
    await cache.delete(response_cache_key(key))
    state = await _run(ctx)
    assert state.extraction is None
    assert len(provider.calls) == 1, "response changes must not replay committed graph writes"


async def test_incomplete_assertions_do_not_abort_valid_graph_writes(db_conn, novel):
    response = json.loads(RESPONSE)
    response["facts"].append({"entity": "Li Xiaoyao", "attribute": "status", "value": None})
    response["edges"].append({"src": "Li Xiaoyao", "dst": "Azure Cloud Sect", "rel_type": ""})
    provider = FakeProvider(json.dumps(response))
    ctx = _ctx(db_conn, novel, provider, LLMCache(FakeRedis()))
    resolutions = await seed_entities(db_conn, novel, CAST)
    state = await _run(ctx, resolutions=resolutions)
    assert state.extraction.discarded_rows == {"facts": 1, "edges": 1}
    assert await _counts(db_conn, novel) == {"entity": 2, "fact": 1, "edge": 1, "event": 1}
    await _run(ctx, resolutions=resolutions)
    assert len(provider.calls) == 1
    assert (await _counts(db_conn, novel))["fact"] == 1


async def test_skipped_stage_writes_nothing_but_an_empty_extraction_is_not_a_skip(db_conn, novel):
    """``extraction is None`` (stage skipped, already written) and an empty
    ``Extraction`` (model found nothing) must not be conflated — the first must write
    nothing, the second is a legitimate zero-knowledge chapter that still completes."""
    empty = FakeProvider(json.dumps({"entities": [], "facts": [], "edges": [], "events": []}))
    cache = LLMCache(FakeRedis())
    ctx = _ctx(db_conn, novel, empty, cache)

    state = await _run(ctx)
    assert state.extraction is not None and state.extraction.facts == []
    assert (await _counts(db_conn, novel))["fact"] == 0

    # Second run: the job is done, so the stage returns without output at all.
    state = await _run(ctx)
    assert state.extraction is None


async def test_unresolved_surface_is_dropped_and_counted(db_conn, novel):
    """The 1.6 binding contract: graph-write does no name matching of its own, so a
    surface RESOLVE never bound is dropped rather than guessed at. Seeding only half the
    cast is exactly the extraction/resolution disagreement the eval set measures."""
    partial = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    await _run(
        _ctx(db_conn, novel, FakeProvider(RESPONSE), LLMCache(FakeRedis())),
        resolutions=partial,
    )

    counts = await _counts(db_conn, novel)
    assert counts["fact"] == 1  # the Li Xiaoyao fact survives
    assert counts["edge"] == 0  # the edge needs Azure Cloud Sect, which never resolved
    assert counts["entity"] == 1, "graph-write must not create the missing entity"


async def test_ontology_edit_forces_re_extraction(db_conn, novel):
    """The ontology is in the key (§3.5), so adding a tracked relation must re-run the
    chapter rather than serve an extraction produced by a prompt that never asked for
    it."""
    provider = FakeProvider(RESPONSE)
    cache = LLMCache(FakeRedis())

    await _run(_ctx(db_conn, novel, provider, cache))
    richer = {**ONTOLOGY, "relations": ["member_of", "enemy"]}
    await _run(_ctx(db_conn, novel, provider, cache, ontology=richer))

    assert len(provider.calls) == 2


# --- what the extractor is and isn't trusted with ----------------------------


async def test_flashback_story_time_is_preserved(db_conn, novel):
    """A revelation about the past: story-time 10, knowledge-time 41. Both columns must
    survive intact — collapsing them is §12 risk #1, and the gate reads the one the
    extractor does NOT control."""
    response = json.dumps(
        {
            "entities": [{"surface": "Li Xiaoyao", "kind": "character"}],
            "facts": [
                {
                    "entity": "Li Xiaoyao",
                    "attribute": "rank",
                    "value": "expelled disciple",
                    "valid_from_chapter": 10,
                }
            ],
        }
    )
    resolutions = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    await _run(
        _ctx(db_conn, novel, FakeProvider(response), LLMCache(FakeRedis())),
        resolutions=resolutions,
    )

    row = await (
        await db_conn.execute(
            "SELECT valid_from_chapter, source_chapter FROM fact WHERE novel_id = %s", (novel,)
        )
    ).fetchone()
    assert row == (10, CHAPTER)


async def test_story_time_after_knowledge_time_is_clamped(db_conn, novel):
    """A hallucinated future story-time is nonsense on the timeline. It cannot leak
    anything (the gate is source_chapter), so clamping beats discarding the chapter."""
    response = json.dumps(
        {
            "entities": [{"surface": "Li Xiaoyao", "kind": "character"}],
            "facts": [
                {
                    "entity": "Li Xiaoyao",
                    "attribute": "rank",
                    "value": "Core Formation",
                    "valid_from_chapter": CHAPTER + 500,
                }
            ],
        }
    )
    resolutions = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    await _run(
        _ctx(db_conn, novel, FakeProvider(response), LLMCache(FakeRedis())),
        resolutions=resolutions,
    )

    row = await (
        await db_conn.execute(
            "SELECT valid_from_chapter, source_chapter FROM fact WHERE novel_id = %s", (novel,)
        )
    ).fetchone()
    assert row == (CHAPTER, CHAPTER)


async def test_fact_about_an_undeclared_surface_is_dropped(db_conn, novel):
    """Binding a surface the extractor never declared would invent an entity, which
    pollutes the resolver's candidate set for every later chapter — worse than a missing
    fact. Since 1.6 the mechanism is the resolutions map rather than a declared-entities
    check, but the guarantee is the same one."""
    response = json.dumps(
        {
            "entities": [{"surface": "Li Xiaoyao", "kind": "character"}],
            "facts": [
                {"entity": "Li Xiaoyao", "attribute": "rank", "value": "Qi Refining"},
                {"entity": "Someone Unmentioned", "attribute": "rank", "value": "?"},
            ],
        }
    )
    resolutions = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    await _run(
        _ctx(db_conn, novel, FakeProvider(response), LLMCache(FakeRedis())),
        resolutions=resolutions,
    )

    counts = await _counts(db_conn, novel)
    assert counts["fact"] == 1
    assert counts["entity"] == 1


async def test_ontology_invalid_rows_and_partial_events_fail_closed_but_valid_rows_survive(db_conn,novel):
    response=json.dumps({
        "entities":[{"surface":"Li Xiaoyao","kind":"character"},
                    {"surface":"Azure Cloud Sect","kind":"sect"},
                    {"surface":"Ghost","kind":"invented-kind"}],
        "facts":[{"entity":"Li Xiaoyao","attribute":"rank","value":"Qi Refining"},
                 {"entity":"Azure Cloud Sect","attribute":"rank","value":"invalid kind"},
                 {"entity":"Li Xiaoyao","attribute":"invented","value":"invalid attr"}],
        "edges":[{"src":"Li Xiaoyao","dst":"Azure Cloud Sect","rel_type":"member_of"},
                 {"src":"Li Xiaoyao","dst":"Azure Cloud Sect","rel_type":"invented"}],
        "events":[{"summary":"valid","entities":["Li Xiaoyao"]},
                  {"summary":"must be discarded whole","entities":["Li Xiaoyao","Undeclared"]}]})
    resolutions=await seed_entities(db_conn,novel,{"Li Xiaoyao":"character","Azure Cloud Sect":"sect","Ghost":"character"})
    await _run(_ctx(db_conn,novel,FakeProvider(response),LLMCache(FakeRedis())),resolutions=resolutions)
    counts=await _counts(db_conn,novel)
    assert counts["fact"]==1 and counts["edge"]==1 and counts["event"]==1


async def test_failover_result_is_used_but_not_cached(db_conn, novel):
    """§12 / §14.3 at the stage level: a response served by another model is still valid
    output to write, but it must not be stored under this key. The next run therefore
    calls the model again rather than serving a mis-attributed result."""
    provider = FakeProvider(RESPONSE, served_model="llama3.1:8b")
    redis = FakeRedis()
    ctx = _ctx(db_conn, novel, provider, LLMCache(redis))

    await _run(ctx, stages=(StateStage(),))
    assert (await _counts(db_conn, novel))["fact"] == 0  # state stage alone writes nothing
    assert redis.store == {}, "a failover response must not be cached (§12)"

    await _run(ctx, stages=(StateStage(),))
    assert len(provider.calls) == 2
