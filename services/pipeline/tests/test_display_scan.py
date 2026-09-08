"""DISPLAY SCAN (instructions.md §5 step 6; PLAN.md Phase 5.2).

Two branches, tested separately: translated novels scan the LOCKED GLOSSARY TERMS
against the translated text (a different string from the source, so a fresh scan is
required); untranslated novels reuse the extraction-time scan verbatim, since the
displayed text and the source text are byte-identical.

Needs a live Postgres for the glossary query — skipped cleanly via db_conn when
unreachable, same as test_resolve.py.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel, seed_entities

from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.mentions import Span
from pipeline.stages.display_scan import DisplayScanStage

pytestmark = pytest.mark.db

CHAPTER = 12
ONTOLOGY = {"kinds": ["character", "sect"], "attributes": [], "relations": []}


def _ctx(db, novel_id, *, source_lang: str, target_lang: str) -> StageContext:
    provider = FakeProvider('{"names": []}')
    return StageContext(
        novel=NovelMeta(id=novel_id, source_lang=source_lang, target_lang=target_lang, ontology=ONTOLOGY),
        language_profile=language_profile_for(source_lang),
        provider=provider,
        batch_manager=BatchManager(provider),
        embed_provider=provider,
        db=db,
        objects=None,
        cfg=make_config(),
        cache=LLMCache(FakeRedis()),
    )


def _state(*, source_lang: str, translation: str | None = None,
           chapter_index: int = CHAPTER) -> PipelineState:
    return PipelineState(
        envelope=ChapterEnvelope(
            novel_id="ignored",
            chapter_index=chapter_index,
            raw_text="他回到了青云宗。",
            source_lang=source_lang,
            source_meta=SourceMeta(raw_hash="sha256:cafe"),
        ),
        translation=translation,
    )


@pytest.fixture
async def novel(db_conn):
    novel_id = await make_novel(db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY))
    await db_conn.execute(
        "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
        "VALUES (%s,%s,%s,'raw/test.txt','{}','done')",
        (novel_id, CHAPTER, f"sha256:{novel_id}"),
    )
    try:
        yield novel_id
    finally:
        await delete_novel(db_conn, novel_id)


async def test_translated_novel_scans_the_translated_text_against_the_glossary(db_conn, novel):
    entity_id = str(uuid.uuid4())
    await db_conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, 'sect', 'Azure Cloud Sect', 1)",
        (entity_id, novel),
    )
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, entity_id, locked_at_chapter) "
        "VALUES (%s, '青云宗', 'Azure Cloud Sect', %s, 1)",
        (novel, entity_id),
    )
    revision_id = (await (await db_conn.execute(
        "SELECT active_graph_revision FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await db_conn.execute(
        "INSERT INTO glossary_binding(novel_id,source_term,revision_id,entity_id,known_from_chapter) "
        "VALUES (%s,'青云宗',%s,%s,1)", (novel, revision_id, entity_id))

    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    translated = "He returned to the Azure Cloud Sect."
    state = _state(source_lang="zh", translation=translated)

    await DisplayScanStage().run(ctx, state)

    [span] = state.display_spans
    assert span.alias_id == entity_id
    assert translated[span.char_start : span.char_end] == "Azure Cloud Sect"


async def test_translated_novel_keeps_glossary_rows_with_no_bound_entity_as_placeholders(db_conn, novel):
    """A pre-identity glossary row still supplies a display span, but no entity link."""
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, locked_at_chapter) "
        "VALUES (%s, '青云宗', 'Azure Cloud Sect', 1)",
        (novel,),
    )
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    state = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.")

    await DisplayScanStage().run(ctx, state)

    [span] = state.display_spans
    assert span.alias_id == ""
    assert state.translation[span.char_start : span.char_end] == "Azure Cloud Sect"


async def test_legacy_glossary_links_are_knowledge_time_gated(db_conn, novel):
    await db_conn.execute(
        "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
        "VALUES (%s,9,%s,'raw/early.txt','{}','done')",
        (novel, f"sha256:{novel}:early"),
    )
    entity_id = str(uuid.uuid4())
    await db_conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, 'sect', 'Azure Cloud Sect', 10)",
        (entity_id, novel),
    )
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, entity_id, locked_at_chapter) "
        "VALUES (%s, '青云宗', 'Azure Cloud Sect', %s, 10)",
        (novel, entity_id),
    )
    revision_id = (await (await db_conn.execute(
        "SELECT active_graph_revision FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await db_conn.execute(
        "INSERT INTO glossary_binding(novel_id,source_term,revision_id,entity_id,known_from_chapter) "
        "VALUES (%s,'青云宗',%s,%s,10)", (novel, revision_id, entity_id))
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")

    early = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.",
                   chapter_index=9)
    await DisplayScanStage().run(ctx, early)
    [early_span] = early.display_spans
    assert early_span.alias_id == ""

    eligible = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.",
                      chapter_index=12)
    await DisplayScanStage().run(ctx, eligible)
    [eligible_span] = eligible.display_spans
    assert eligible_span.alias_id == entity_id


async def test_translate_skipped_leaves_no_display_spans(db_conn, novel):
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    state = _state(source_lang="zh", translation=None)

    await DisplayScanStage().run(ctx, state)

    assert state.display_spans == []


async def test_untranslated_novel_reuses_the_extraction_time_scan(db_conn, novel):
    ctx = _ctx(db_conn, novel, source_lang="en", target_lang="en")
    state = _state(source_lang="en")
    ids = await seed_entities(db_conn, novel, {"他回": "character"})
    state.mentions = [Span(alias_id=ids["他回"], byte_start=0, byte_end=6, char_start=0, char_end=2)]

    await DisplayScanStage().run(ctx, state)

    assert state.display_spans == state.mentions


async def test_unlinked_names_publish_before_facts_and_do_not_create_entities(db_conn, novel):
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    ctx.provider.response = lambda _prompt, system: (
        '{"alignments":[{"display_term":"Ling Feng","source_term":"他"},'
        '{"display_term":"Black Tower","source_term":"青云宗"}]}'
        if "Map each offered" in system else
        '{"names": ["Ling Feng", "Black Tower", "Not in the text"]}')
    state = _state(source_lang="zh", translation="Ling Feng entered the Black Tower.")
    await DisplayScanStage().run(ctx, state)
    await DisplayScanStage().run(ctx, state)
    rows = await (await db_conn.execute(
        "SELECT entity_id, char_start, char_end FROM mention_span WHERE novel_id=%s ORDER BY char_start",
        (novel,),
    )).fetchall()
    assert rows == [(None, 0, 9), (None, 22, 33)]
    aligned = await (await db_conn.execute(
        "SELECT source_term,display_term,char_start,char_end FROM term_rendering_occurrence "
        "WHERE novel_id=%s ORDER BY char_start", (novel,))).fetchall()
    assert aligned == [("他", "Ling Feng", 0, 9), ("青云宗", "Black Tower", 22, 33)]
    assert await (await db_conn.execute("SELECT count(*) FROM entity WHERE novel_id=%s", (novel,))).fetchone() == (0,)
    assert len(ctx.provider.calls) == 2  # one discovery + one alignment; rerun is cached


async def test_discovery_preserves_verified_link_and_does_not_link_other_names(db_conn, novel):
    entity_id = str(uuid.uuid4())
    await db_conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) VALUES (%s,%s,'sect','Azure Cloud Sect',1)",
        (entity_id, novel),
    )
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, entity_id, locked_at_chapter) VALUES (%s,'青云宗','Azure Cloud Sect',%s,1)",
        (novel, entity_id),
    )
    revision_id = (await (await db_conn.execute(
        "SELECT active_graph_revision FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await db_conn.execute(
        "INSERT INTO glossary_binding(novel_id,source_term,revision_id,entity_id,known_from_chapter) "
        "VALUES (%s,'青云宗',%s,%s,1)", (novel, revision_id, entity_id))
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    ctx.provider.response = lambda _prompt, system: (
        '{"alignments":[]}' if "Map each offered" in system
        else '{"names": ["Azure Cloud Sect", "Ling Feng"]}')
    state = _state(source_lang="zh", translation="Ling Feng joined Azure Cloud Sect.")
    await DisplayScanStage().run(ctx, state)
    assert [(s.alias_id, state.translation[s.char_start:s.char_end]) for s in state.display_spans] == [
        ("", "Ling Feng"), (entity_id, "Azure Cloud Sect")]


async def test_legacy_links_follow_active_trusted_revision_only(db_conn, novel):
    """Legacy spans are presentation fallbacks; managed revision ids never enter them."""
    entity_id = str(uuid.uuid4())
    await db_conn.execute(
        "INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, 'sect', 'Azure Cloud Sect', 1)",
        (entity_id, novel),
    )
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, entity_id, locked_at_chapter) "
        "VALUES (%s, '青云宗', 'Azure Cloud Sect', %s, 1)",
        (novel, entity_id),
    )
    legacy = (await (await db_conn.execute(
        "SELECT active_graph_revision FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await db_conn.execute(
        "INSERT INTO glossary_binding(novel_id,source_term,revision_id,entity_id,known_from_chapter) "
        "VALUES (%s,'青云宗',%s,%s,1)", (novel, legacy, entity_id))
    managed = (await (await db_conn.execute(
        "INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology) "
        "VALUES (%s,'staging',false,false,%s) RETURNING id", (novel, json.dumps(ONTOLOGY))
    )).fetchone())[0]
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")

    async def scan() -> str:
        state = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.")
        await DisplayScanStage().run(ctx, state)
        [span] = state.display_spans
        return span.alias_id

    # The original legacy graph may link a verified binding.
    assert await scan() == entity_id

    # No active revision and a staging/quarantined active revision can still write the
    # presentation ledger, but only as empty placeholders.
    await db_conn.execute("UPDATE novel SET active_graph_revision=NULL WHERE id=%s", (novel,))
    assert await scan() == ""
    await db_conn.execute("UPDATE novel SET active_graph_revision=%s WHERE id=%s", (managed, novel))
    assert await scan() == ""
    await db_conn.execute("UPDATE novel SET active_graph_revision=%s WHERE id=%s", (legacy, novel))
    await db_conn.execute("UPDATE graph_revision SET trusted=false WHERE id=%s", (legacy,))
    assert await scan() == ""

    # Cutover to a trusted managed revision still cannot make its binding eligible for
    # legacy mention_span. Managed identity is reader-authorized through its own tables.
    await db_conn.execute("UPDATE graph_revision SET state='archived' WHERE id=%s", (legacy,))
    await db_conn.execute(
        "UPDATE graph_revision SET state='active',trusted=true WHERE id=%s", (managed,))
    await db_conn.execute("UPDATE novel SET active_graph_revision=%s WHERE id=%s", (managed, novel))
    assert await scan() == ""


async def test_unlinked_mentions_are_still_chapter_gated_by_rls(db_conn, novel):
    await db_conn.execute(
        "INSERT INTO mention_span(novel_id,chapter_index,entity_id,char_start,char_end) VALUES (%s,12,NULL,0,3)",
        (novel,),
    )
    for at, expected in [(11, []), (12, [(None,)])]:
        async with db_conn.transaction():
            await db_conn.execute("SET LOCAL ROLE rls_reader")
            await db_conn.execute("SELECT set_config('app.novel_id', %s, true), set_config('app.current_chapter', %s, true)", (novel, str(at)))
            rows = await (await db_conn.execute("SELECT entity_id FROM mention_span WHERE novel_id=%s", (novel,))).fetchall()
            assert rows == expected
