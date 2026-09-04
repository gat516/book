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
from dataclasses import replace

import pytest
from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel, seed_entities

from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.stages.resolve import ResolveStage, _lock_glossary
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


def _ctx(
    db, novel_id, provider, *, source_lang: str = "en", target_lang: str = "en", **cfg_overrides
) -> StageContext:
    return StageContext(
        novel=NovelMeta(
            id=novel_id,
            source_lang=source_lang,
            target_lang=target_lang,
            ontology=ONTOLOGY,
        ),
        language_profile=language_profile_for(source_lang),
        provider=provider,
        batch_manager=BatchManager(provider),
        embed_provider=provider,
        db=db,
        objects=None,
        cfg=make_config(**cfg_overrides),
        cache=LLMCache(FakeRedis()),
    )


async def _run(ctx, text: str) -> PipelineState:
    state = PipelineState(
        envelope=ChapterEnvelope(
            novel_id=ctx.novel.id,
            chapter_index=CHAPTER,
            raw_text=text,
            source_lang=ctx.novel.source_lang,
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


async def test_success_is_recorded_but_does_not_skip_live_resolution(db_conn, novel):
    provider = FakeProvider(_responder(propose={}, decide={}))
    ctx = _ctx(db_conn, novel, provider)
    await _run(ctx, "An empty room.")
    row = await (await db_conn.execute(
        "SELECT state FROM job WHERE novel_id = %s AND stage = 'resolve'", (novel,)
    )).fetchone()
    assert row == ("done",)

    await _run(ctx, "An empty room.")
    assert len(provider.calls) == 2, "done tracks success, not a content-cache hit"


async def test_resolve_uses_its_phase_budget_provider(db_conn, novel):
    fallback = FakeProvider(_responder(propose={}, decide={}))
    budgeted = FakeProvider(_responder(propose={}, decide={}))
    ctx = replace(_ctx(db_conn, novel, fallback), resolve_provider=budgeted)
    await _run(ctx, "An empty room.")
    assert len(budgeted.calls) == 1
    assert fallback.calls == []


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
    alias = await (
        await db_conn.execute(
            "SELECT first_seen_chapter FROM alias "
            "WHERE entity_id = %s AND surface = %s AND lang = %s",
            (existing_id, "Azure Sect", "en"),
        )
    ).fetchone()
    assert alias == (CHAPTER,)


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


async def test_translated_confirmed_variant_becomes_alias_and_glossary_term(db_conn):
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        known = await seed_entities(
            db_conn, novel_id, {"Azure Cloud Sect": "sect"}, lang="en"
        )
        entity_id = known["Azure Cloud Sect"]
        [vector] = await FakeProvider().embed(["Azure Cloud Sect"])
        await db_conn.execute(
            "UPDATE entity SET embedding = %s WHERE id = %s", (str(vector), entity_id)
        )
        provider = FakeProvider(
            _responder(
                propose={"青云门": "sect"},
                decide={
                    "青云门": {"decision": "confirm", "entity_id": entity_id}
                },
            )
        )

        state = await _run(
            _ctx(db_conn, novel_id, provider, source_lang="zh", target_lang="en"),
            "他回到了青云门。",
        )

        alias = await (
            await db_conn.execute(
                "SELECT first_seen_chapter FROM alias "
                "WHERE entity_id = %s AND surface = %s AND lang = %s",
                (entity_id, "青云门", "zh"),
            )
        ).fetchone()
        glossary = await (
            await db_conn.execute(
                "SELECT target_term, entity_id FROM glossary "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云门"),
            )
        ).fetchone()

        assert state.resolutions["青云门"] == entity_id
        assert alias == (CHAPTER,)
        assert (glossary[0], str(glossary[1])) == ("Azure Cloud Sect", entity_id)
    finally:
        await delete_novel(db_conn, novel_id)


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
        _responder(
            propose={"Li Xiaoyao": "character"},
            decide={
                "Li Xiaoyao": {
                    "decision": "new",
                    # Same-language resolution must ignore an unsolicited generated name.
                    "target_term": "Wanderer Li",
                }
            },
        )
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


async def test_translated_new_entity_locks_target_term_and_audit(db_conn):
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        provider = FakeProvider(
            _responder(
                propose={"青云宗": "sect"},
                decide={
                    "青云宗": {
                        "decision": "new",
                        "target_term": "Azure Cloud Sect",
                    }
                },
            )
        )
        state = await _run(
            _ctx(db_conn, novel_id, provider, source_lang="zh", target_lang="en"),
            "他回到了青云宗。",
        )

        entity_id = state.resolutions["青云宗"]
        entity = await (
            await db_conn.execute(
                "SELECT canonical FROM entity WHERE id = %s", (entity_id,)
            )
        ).fetchone()
        glossary = await (
            await db_conn.execute(
                "SELECT target_term, entity_id, version FROM glossary "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()
        audit = await (
            await db_conn.execute(
                "SELECT new_target, seq FROM glossary_changelog "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()

        assert entity == ("Azure Cloud Sect",)
        assert (glossary[0], str(glossary[1]), glossary[2]) == (
            "Azure Cloud Sect",
            entity_id,
            1,
        )
        assert audit == ("Azure Cloud Sect", 1)

        # An idempotent lock must not fabricate another audit entry.
        async with db_conn.transaction():
            await _lock_glossary(
                db_conn,
                novel_id=novel_id,
                source_term="青云宗",
                target_term="Azure Cloud Sect",
                entity_id=entity_id,
                chapter=CHAPTER,
            )
        audit_count = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary_changelog "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()
        assert audit_count == (1,)
    finally:
        await delete_novel(db_conn, novel_id)


async def test_bootstrap_glossary_term_is_backfilled_not_reproposed(db_conn):
    """PLAN.md Phase N6: a glossary-bootstrap seed (POST .../glossary/bootstrap) locks a
    term BEFORE any entity exists for it — entity_id NULL, locked_at_chapter=0. The first
    time RESOLVE actually meets that surface, it must (a) use the human-locked target_term
    structurally, ignoring anything the model proposes, and (b) backfill entity_id onto
    the existing row rather than raising _lock_glossary's mismatch ValueError against a
    NULL entity_id (which is "not yet bound", not a real conflict)."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        async with db_conn.transaction():
            await db_conn.execute(
                "INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter) "
                "VALUES (%s, %s, %s, 1, 0)",
                (novel_id, "青云宗", "Azure Cloud Sect"),
            )
            await db_conn.execute(
                "INSERT INTO glossary_changelog "
                "(novel_id, seq, source_term, old_target, new_target, changed_at_chapter, prev_hash, row_hash) "
                "VALUES (%s, 1, %s, NULL, %s, 0, NULL, 'seedhash')",
                (novel_id, "青云宗", "Azure Cloud Sect"),
            )

        # The model tries to propose a DIFFERENT target_term — proving the locked seed
        # wins structurally, not merely as an unenforced prompt suggestion.
        provider = FakeProvider(
            _responder(
                propose={"青云宗": "sect"},
                decide={"青云宗": {"decision": "new", "target_term": "Blue Cloud Sect"}},
            )
        )
        state = await _run(
            _ctx(db_conn, novel_id, provider, source_lang="zh", target_lang="en"),
            "他回到了青云宗。",
        )

        entity_id = state.resolutions["青云宗"]
        entity = await (
            await db_conn.execute("SELECT canonical FROM entity WHERE id = %s", (entity_id,))
        ).fetchone()
        glossary = await (
            await db_conn.execute(
                "SELECT target_term, entity_id, version FROM glossary "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()
        changelog_count = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary_changelog WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()

        # Locked seed wins, not the model's "Blue Cloud Sect" proposal.
        assert entity == ("Azure Cloud Sect",)
        assert (glossary[0], str(glossary[1]), glossary[2]) == ("Azure Cloud Sect", entity_id, 1)
        # No new changelog entry: this is a backfill of the existing bootstrap row, not a
        # new lock — version and audit trail are untouched, exactly Phase N6's "Done when".
        assert changelog_count == (1,)
    finally:
        await delete_novel(db_conn, novel_id)


# --- glossary poisoning guards (migration 0016) ------------------------------
#
# A locked term is immutable and enforced against every later translation, so a bad one is
# unrecoverable: the observed failure had a small model return one invented name for six
# different source terms, after which no correct translation could ever satisfy the
# glossary and the novel became permanently untranslatable. These guard the write path so
# model weakness degrades quality instead of bricking a novel.


async def test_duplicate_target_term_is_refused(db_conn):
    """Guard 1: one target term per novel. The six-way collapse is exactly this."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        ids = await seed_entities(db_conn, novel_id, {"凌峰": "character", "姜梦月": "character"})
        async with db_conn.transaction():
            first = await _lock_glossary(
                db_conn, novel_id=novel_id, source_term="凌峰", target_term="Ling Feng",
                entity_id=ids["凌峰"], chapter=1, target_lang="en", min_proposals=1,
            )
        assert first == 1

        # A different source claiming the same target must be declined, not locked.
        async with db_conn.transaction():
            second = await _lock_glossary(
                db_conn, novel_id=novel_id, source_term="姜梦月", target_term="Ling Feng",
                entity_id=ids["姜梦月"], chapter=1, target_lang="en", min_proposals=1,
            )
        assert second is None

        rows = await (
            await db_conn.execute(
                "SELECT source_term FROM glossary WHERE novel_id = %s", (novel_id,)
            )
        ).fetchall()
        assert [r[0] for r in rows] == ["凌峰"], "the first claim must survive intact"
    finally:
        await delete_novel(db_conn, novel_id)


async def test_untranslated_target_term_is_refused(db_conn):
    """Guard 2: for zh->en, a target still in the source script means the model didn't
    translate it — echoing the source back, or emitting a placeholder name."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        ids = await seed_entities(db_conn, novel_id, {"青云宗": "sect"})
        async with db_conn.transaction():
            locked = await _lock_glossary(
                db_conn, novel_id=novel_id, source_term="青云宗", target_term="青云宗",
                entity_id=ids["青云宗"], chapter=1, target_lang="en", min_proposals=1,
            )
        assert locked is None

        count = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary WHERE novel_id = %s", (novel_id,)
            )
        ).fetchone()
        assert count == (0,)
    finally:
        await delete_novel(db_conn, novel_id)


async def test_term_locks_only_after_a_second_chapter_agrees(db_conn):
    """Guard 3: corroboration. One chapter's proposal is held provisionally; a second,
    independent chapter proposing the same mapping promotes it."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        ids = await seed_entities(db_conn, novel_id, {"青云宗": "sect"})
        kwargs = dict(
            novel_id=novel_id, source_term="青云宗", target_term="Azure Cloud Sect",
            entity_id=ids["青云宗"], target_lang="en", min_proposals=2,
        )

        async with db_conn.transaction():
            assert await _lock_glossary(db_conn, chapter=1, **kwargs) is None

        locked = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary WHERE novel_id = %s", (novel_id,)
            )
        ).fetchone()
        assert locked == (0,), "first sighting must not lock"

        # Re-running the SAME chapter is not independent evidence and must not promote it.
        async with db_conn.transaction():
            assert await _lock_glossary(db_conn, chapter=1, **kwargs) is None
        still_unlocked = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary WHERE novel_id = %s", (novel_id,)
            )
        ).fetchone()
        assert still_unlocked == (0,), "a chapter must not corroborate itself"

        async with db_conn.transaction():
            assert await _lock_glossary(db_conn, chapter=2, **kwargs) == 1

        row = await (
            await db_conn.execute(
                "SELECT target_term FROM glossary WHERE novel_id = %s AND source_term = %s",
                (novel_id, "青云宗"),
            )
        ).fetchone()
        assert row == ("Azure Cloud Sect",)

        # Promotion clears the provisional record it was promoted from.
        leftover = await (
            await db_conn.execute(
                "SELECT count(*) FROM glossary_candidate WHERE novel_id = %s", (novel_id,)
            )
        ).fetchone()
        assert leftover == (0,)
    finally:
        await delete_novel(db_conn, novel_id)


async def test_managed_glossary_lock_keeps_global_entity_null(db_conn):
    novel_id=await make_novel(db_conn,source_lang="zh",target_lang="en",ontology=json.dumps(ONTOLOGY))
    try:
        kwargs=dict(novel_id=novel_id,source_term="九神殿",target_term="Dream Palace",
                    entity_id=None,target_lang="en",min_proposals=2)
        async with db_conn.transaction():
            assert await _lock_glossary(db_conn,chapter=1,**kwargs) is None
        async with db_conn.transaction():
            assert await _lock_glossary(db_conn,chapter=2,**kwargs)==1
        assert await (await db_conn.execute("""SELECT target_term,entity_id,locked_at_chapter
            FROM glossary WHERE novel_id=%s AND source_term='九神殿'""",(novel_id,))).fetchone()==("Dream Palace",None,2)
    finally:
        await delete_novel(db_conn,novel_id)


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


async def test_retry_reuses_same_chapter_entity_for_repeated_surface(db_conn, novel):
    """A failed attempt may have committed RESOLVE's idempotent entity/alias writes
    before its job marker. On retry, replaying one model call per occurrence can exceed
    the claim lease and restart the same chapter forever. An entity first seen in this
    chapter is the result the initial no-candidate pass already chose, so resume it for
    every occurrence without weakening the cross-chapter homonym check (§5, §6.3)."""
    known = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    await db_conn.execute(
        "UPDATE entity SET first_seen_chapter=%s WHERE id=%s",
        (CHAPTER, known["Li Xiaoyao"]),
    )
    provider = FakeProvider(_responder(propose={"Li Xiaoyao": "character"}, decide={}))

    state = await _run(
        _ctx(db_conn, novel, provider),
        "Li Xiaoyao drew his sword. Li Xiaoyao advanced. Li Xiaoyao smiled.",
    )

    assert state.resolutions["Li Xiaoyao"] == known["Li Xiaoyao"]
    assert len(provider.calls) == 1, "same-chapter resume must not replay occurrence calls"


async def test_retry_drops_same_chapter_identity_conflict_without_making_more(db_conn, novel):
    """Contradictory partial decisions are not evidence for choosing either identity.
    Retrying per occurrence would grow the conflict forever, so fail closed and let the
    rest of the chapter proceed (§0.2, §5, §6.3)."""
    first = await seed_entities(db_conn, novel, {"Li Xiaoyao": "character"})
    second = await seed_entities(db_conn, novel, {"Other Li": "character"})
    await db_conn.execute(
        "UPDATE entity SET first_seen_chapter=%s WHERE id = ANY(%s::uuid[])",
        (CHAPTER, [first["Li Xiaoyao"], second["Other Li"]]),
    )
    await db_conn.execute(
        "INSERT INTO alias (entity_id,surface,lang,first_seen_chapter) VALUES (%s,%s,%s,%s)",
        (second["Other Li"], "Li Xiaoyao", "en", CHAPTER),
    )
    provider = FakeProvider(_responder(propose={"Li Xiaoyao": "character"}, decide={}))

    state = await _run(
        _ctx(db_conn, novel, provider),
        "Li Xiaoyao drew his sword. Li Xiaoyao smiled.",
    )

    assert "Li Xiaoyao" not in state.resolutions
    assert len(provider.calls) == 1, "retry conflict must not create another identity"
    assert len(await _entities(db_conn, novel)) == 2


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


async def test_human_deleted_term_cannot_be_automatically_relocked(db_conn):
    from pipeline.stages.resolve import _locked_target, _target_term_owner
    novel_id = await make_novel(db_conn)
    try:
        await db_conn.execute(
            "INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter, deleted) "
            "VALUES (%s, '凌峰', 'Ling Feng', 3, 0, true)", (novel_id,),
        )
        assert await _locked_target(db_conn, novel_id, '凌峰') is None
        assert await _target_term_owner(db_conn, novel_id, 'Ling Feng') is None
        async with db_conn.transaction():
            assert await _lock_glossary(db_conn, novel_id=novel_id, source_term='凌峰',
                                       target_term='Ling Feng', entity_id=None, chapter=4,
                                       require_corroboration=False) is None
        row = await (await db_conn.execute(
            'SELECT version, deleted FROM glossary WHERE novel_id=%s', (novel_id,),
        )).fetchone()
        assert row == (3, True)
    finally:
        await delete_novel(db_conn, novel_id)


def test_source_term_problem_rejects_surfaces_that_cannot_be_substituted():
    """Guard on the SOURCE side, the twin of _target_term_problem.

    Locked terms are substituted into the chapter before translation
    (translation.prime_glossary_terms), and CJK has no word delimiters, so an unusable
    surface here corrupts the text being translated rather than merely costing a
    constraint. The prose cases are real rows from this repo's own alias table.
    """
    from pipeline.stages.resolve import _source_term_problem

    assert _source_term_problem("九神殿") is None
    assert _source_term_problem("青云宗") is None
    assert _source_term_problem("Azure Cloud Sect") is None
    # An ASCII period is legitimate inside a name and must not read as sentence punctuation.
    assert _source_term_problem("St. Mary") is None
    assert _source_term_problem("  九神殿  ") is None

    # One character is a morpheme, not a name: locking 神 rewrites 精神 into "精God".
    assert "too short" in _source_term_problem("神")
    assert "too short" in _source_term_problem("A")
    assert "prose" in _source_term_problem("神职细分为四个层次：主宰，一级神职。")
    assert "prose" in _source_term_problem("九神殿的大门打开了。")
    assert "prose" in _source_term_problem("two\nlines")
    assert "prose" in _source_term_problem("x" * 90)


async def test_lock_glossary_declines_a_single_character_source_term(db_conn):
    """Declining is safe; locking is not. The surface simply gets no locked term."""
    novel_id = await make_novel(db_conn)
    try:
        async with db_conn.transaction():
            assert await _lock_glossary(
                db_conn, novel_id=novel_id, source_term="神", target_term="God",
                entity_id=None, chapter=1, require_corroboration=False,
            ) is None
        count = await (await db_conn.execute(
            "SELECT count(*) FROM glossary WHERE novel_id = %s", (novel_id,),
        )).fetchone()
        assert count[0] == 0
    finally:
        await delete_novel(db_conn, novel_id)


def test_entity_canonical_rejects_the_half_translated_names_the_graph_actually_stored():
    """_target_term_problem now also guards EntityRow.canonical in _decide, not just a
    locked glossary term.

    These are real canonicals from this repo's own entity table for chapter 1. They were
    stored because the check was applied to the glossary only, so the glossary was
    structurally protected from a string the entity table accepted — the same value,
    refused in one place and kept in the other. A half-translated canonical is entity
    drift (§12 risk #2) with no crash to announce it.
    """
    from pipeline.stages.resolve import _target_term_problem

    # Half English, half Chinese: 六纪星 was substituted and 莲 was orphaned.
    assert _target_term_problem("Hexalinear Star莲", "en") is not None
    # A fully rendered name of the same concept stays acceptable.
    assert _target_term_problem("Sixth Era Star Lotus", "en") is None
    # The orphaned character transliterated rather than translated is NOT caught by the
    # script check — it is pure ASCII. Recorded so the limit of this guard is explicit:
    # it catches "failed to translate at all", not "translated the wrong way".
    assert _target_term_problem("Hexalinear Star Lian", "en") is None
    # A CJK target language keeps CJK canonicals, so the guard stays language-relative.
    assert _target_term_problem("六纪星莲", "zh") is None
