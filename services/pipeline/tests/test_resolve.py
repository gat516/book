"""RESOLVE — retrieve-then-resolve (instructions.md §5, §12 risk #2; PLAN.md 1.6).

The reason this stage exists is one failure mode: **entity drift**. A resolver that
free-generates a canonical name per chapter turns "Azure Cloud Sect", "Blue Cloud Sect"
and "Qingyun Sect" into three entities by chapter 600. Nothing throws, no test goes red,
and every downstream feature is quietly corrupt. So the load-bearing test here is
``test_variant_spelling_resolves_to_the_existing_entity`` — the rest guard the machinery
that makes it possible.

The second property worth stating: "never free-generates" is enforced by the *parser*,
not by the prompt. A model that returns an entity id it was never offered produces a
``FreeGeneratedEntity`` and the surface is left unresolved, rather than binding to a
fabricated id that may not even exist.

Needs a live Postgres — skipped cleanly via db_conn (conftest.py) when unreachable. The
LLM and its embeddings are fakes; nothing here talks to a network.
"""

from __future__ import annotations

import json

import pytest
from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel, seed_entities

from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.stages.resolve import ResolveStage
from pipeline.stages.scan import ScanStage

pytestmark = pytest.mark.db

CHAPTER = 41
ONTOLOGY = {
    "kinds": ["character", "sect"],
    "attributes": [{"name": "rank", "kinds": ["character"]}],
    "relations": ["member_of"],
}


def _responder(*, propose: dict[str, str], decide: dict[str, dict]):
    """Script RESOLVE's two different asks.

    ``propose`` is {surface: kind} for the proposal pass; ``decide`` is
    {surface: decision-object} for the per-surface disambiguation. Dispatch is on the
    presence of a candidate list, which is what actually distinguishes the two prompts.
    """

    def respond(prompt: str, system: str) -> str:
        if "Candidates:" not in prompt:
            return json.dumps(
                {"mentions": [{"surface": s, "kind": k} for s, k in propose.items()]}
            )
        surface = prompt.split("Name: ", 1)[1].split("\n", 1)[0].strip()
        return json.dumps(decide.get(surface, {"decision": "new"}))

    return respond


def _ctx(db, novel_id, provider) -> StageContext:
    return StageContext(
        novel=NovelMeta(id=novel_id, source_lang="en", target_lang="en", ontology=ONTOLOGY),
        language_profile=language_profile_for("en"),
        provider=provider,
        embed_provider=provider,
        db=db,
        objects=None,
        cfg=make_config(),
        cache=LLMCache(FakeRedis()),
    )


async def _run(ctx, text: str) -> PipelineState:
    state = PipelineState(
        envelope=ChapterEnvelope(
            novel_id=ctx.novel.id,
            chapter_index=CHAPTER,
            raw_text=text,
            source_lang="en",
            source_meta=SourceMeta(raw_hash="sha256:cafe"),
        )
    )
    for stage in (ScanStage(), ResolveStage()):
        await stage.run(ctx, state)
    return state


async def _entities(db, novel_id) -> list[tuple]:
    return await (
        await db.execute(
            "SELECT canonical, kind, first_seen_chapter, embedding IS NOT NULL "
            "FROM entity WHERE novel_id = %s ORDER BY canonical",
            (novel_id,),
        )
    ).fetchall()


@pytest.fixture
async def novel(db_conn):
    novel_id = await make_novel(db_conn, source_lang="en", ontology=json.dumps(ONTOLOGY))
    try:
        yield novel_id
    finally:
        await delete_novel(db_conn, novel_id)


# --- the drift test: the reason this stage exists ---------------------------


async def test_variant_spelling_resolves_to_the_existing_entity(db_conn, novel):
    """§12 risk #2. The chapter calls a known sect by a shorter name. Exact matching —
    what stood in for resolution through 1.5 — would create a second entity here and the
    graph would bifurcate silently. Retrieve-then-resolve offers the known entity as a
    candidate and the model confirms it."""
    known = await seed_entities(db_conn, novel, {"Azure Cloud Sect": "sect"})
    existing_id = known["Azure Cloud Sect"]
    # Give the existing entity an embedding, or vector retrieval cannot reach it.
    [vector] = await FakeProvider().embed(["Azure Cloud Sect"])
    await db_conn.execute(
        "UPDATE entity SET embedding = %s WHERE id = %s", (str(vector), existing_id)
    )

    provider = FakeProvider(
        _responder(
            propose={"Azure Sect": "sect"},
            decide={"Azure Sect": {"decision": "confirm", "entity_id": existing_id}},
        )
    )
    state = await _run(_ctx(db_conn, novel, provider), "He returned to the Azure Sect.")

    assert state.resolutions["Azure Sect"] == existing_id
    assert len(await _entities(db_conn, novel)) == 1, "a variant spelling must not fork the entity"


async def test_disambiguator_is_offered_the_existing_entity(db_conn, novel):
    """The mechanism behind the test above: retrieval must actually put the known entity
    in front of the model. If the candidate list were empty, "confirm" would be
    impossible and drift would be the only available answer."""
    known = await seed_entities(db_conn, novel, {"Azure Cloud Sect": "sect"})
    [vector] = await FakeProvider().embed(["Azure Cloud Sect"])
    await db_conn.execute(
        "UPDATE entity SET embedding = %s WHERE id = %s",
        (str(vector), known["Azure Cloud Sect"]),
    )

    provider = FakeProvider(_responder(propose={"Azure Sect": "sect"}, decide={}))
    await _run(_ctx(db_conn, novel, provider), "He returned to the Azure Sect.")

    disambiguation = [c for c in provider.calls if "Candidates:" in c["prompt"]]
    assert len(disambiguation) == 1
    assert known["Azure Cloud Sect"] in disambiguation[0]["prompt"]


# --- free generation is a parse error, not a style guideline ----------------


async def test_free_generated_id_leaves_the_surface_unresolved(db_conn, novel):
    """An id the model was never offered means it invented identity. Binding to it would
    corrupt the graph permanently and silently; leaving the surface unresolved costs
    whatever depended on it and shows up as a number the eval set reports."""
    provider = FakeProvider(
        _responder(
            propose={"Li Xiaoyao": "character"},
            decide={
                "Li Xiaoyao": {
                    "decision": "confirm",
                    "entity_id": "11111111-2222-3333-4444-555555555555",
                }
            },
        )
    )
    state = await _run(_ctx(db_conn, novel, provider), "Li Xiaoyao drew his sword.")

    assert "Li Xiaoyao" not in state.resolutions
    assert await _entities(db_conn, novel) == []


# --- creating entities ------------------------------------------------------


async def test_unknown_surface_creates_one_entity_with_an_embedding(db_conn, novel):
    """The embedding assertion is the one that matters: without it, the entity is
    invisible to vector retrieval and the NEXT chapter's variant spelling has no
    candidate to confirm — drift returns one chapter later."""
    provider = FakeProvider(
        _responder(propose={"Li Xiaoyao": "character"}, decide={"Li Xiaoyao": {"decision": "new"}})
    )
    state = await _run(_ctx(db_conn, novel, provider), "Li Xiaoyao drew his sword.")

    assert [(c, k, ch, has_emb) for c, k, ch, has_emb in await _entities(db_conn, novel)] == [
        ("Li Xiaoyao", "character", CHAPTER, True)
    ]
    assert state.resolutions["Li Xiaoyao"]

    alias = await (
        await db_conn.execute(
            "SELECT surface, first_seen_chapter FROM alias WHERE entity_id = %s",
            (state.resolutions["Li Xiaoyao"],),
        )
    ).fetchone()
    assert alias == ("Li Xiaoyao", CHAPTER)


async def test_entity_created_earlier_in_a_chapter_is_visible_later_in_it(db_conn, novel):
    """Entities are inserted per decision, not batched at end-of-chapter. Batching would
    mean a chapter introducing a character under two names creates two entities — the
    drift this stage prevents, reintroduced as a write-ordering detail."""
    provider = FakeProvider(
        _responder(
            propose={"Li Xiaoyao": "character", "Xiaoyao": "character"},
            decide={"Li Xiaoyao": {"decision": "new"}},  # "Xiaoyao" falls through below
        )
    )
    ctx = _ctx(db_conn, novel, provider)

    # The second surface's disambiguation must SEE the entity the first one created.
    await _run(ctx, "Li Xiaoyao drew his sword. Xiaoyao smiled.")
    prompts = [c["prompt"] for c in provider.calls if "Candidates:" in c["prompt"]]
    second = next(p for p in prompts if p.startswith("Name: Xiaoyao"))
    assert "Li Xiaoyao" in second, "the entity created moments earlier must be a candidate"


# --- known aliases cost nothing ---------------------------------------------


async def test_scanned_alias_binds_without_disambiguation(db_conn, novel):
    """Pass 1 is free. A surface the alias set already matches unambiguously is resolved
    without asking the model anything — only the one proposal call happens."""
    known = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    provider = FakeProvider(_responder(propose={"Li Xiaoyao": "character"}, decide={}))

    state = await _run(_ctx(db_conn, novel, provider), "Li Xiaoyao drew his sword.")

    assert state.resolutions["Li Xiaoyao"] == known["Li Xiaoyao"]
    assert len(provider.calls) == 1, "an unambiguous known alias must not be disambiguated"
    assert len(await _entities(db_conn, novel)) == 1


async def test_ambiguous_scanned_surface_is_disambiguated(db_conn, novel):
    """Two entities share the surface "Chen". That is not scanner noise to be cleaned up
    — choosing between them is precisely this stage's job, so it must reach the model."""
    known = await seed_entities(db_conn, novel, {"Chen": "character"})
    second = await seed_entities(db_conn, novel, {"Chen the Younger": "character"})
    # Give the second entity the same surface as an alias, creating the ambiguity.
    await db_conn.execute(
        "INSERT INTO alias (entity_id, surface, lang, first_seen_chapter) VALUES (%s, %s, %s, %s)",
        (second["Chen the Younger"], "Chen", "en", 1),
    )

    provider = FakeProvider(
        _responder(
            propose={},
            decide={"Chen": {"decision": "confirm", "entity_id": known["Chen"]}},
        )
    )
    state = await _run(_ctx(db_conn, novel, provider), "Chen bowed.")

    assert state.resolutions["Chen"] == known["Chen"]
    assert any("Candidates:" in c["prompt"] for c in provider.calls)


# --- idempotency and prompt hygiene -----------------------------------------


async def test_rerun_creates_no_duplicate_entities(db_conn, novel):
    """Resolve has no job-done guard, because entity/alias writes are ON CONFLICT DO
    NOTHING upserts. This is the test that says that reasoning is actually true."""
    provider = FakeProvider(
        _responder(propose={"Li Xiaoyao": "character"}, decide={"Li Xiaoyao": {"decision": "new"}})
    )
    ctx = _ctx(db_conn, novel, provider)

    await _run(ctx, "Li Xiaoyao drew his sword.")
    first = await _entities(db_conn, novel)

    # Second run: the alias now exists, so the scanner resolves it for free.
    await _run(ctx, "Li Xiaoyao drew his sword.")
    assert await _entities(db_conn, novel) == first


async def test_chapter_text_stays_out_of_the_cacheable_prefix(db_conn, novel):
    """§6.2, same rule as state-extract: the system block is what providers prefix-cache,
    so chapter text belongs in the user block. Silent when wrong — only the bill moves."""
    text = "Li Xiaoyao drew his sword."
    provider = FakeProvider(_responder(propose={}, decide={}))
    await _run(_ctx(db_conn, novel, provider), text)

    proposal = provider.calls[0]
    assert text in proposal["prompt"]
    assert text not in proposal["system"]
    assert "character" in proposal["system"]  # the ontology IS in the stable prefix
