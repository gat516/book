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


async def _link_term(db_conn, novel, *, entity_chapter: int = 1, locked_at: int = 1,
                     generation=None) -> str:
    """Lock 青云宗 -> Azure Cloud Sect and give it an identity in one generation.

    DISPLAY_SCAN links a glossary term to an entity through ``alias.surface`` now, not
    through the retired ``glossary_binding`` ledger: an alias is what says "this surface
    is that character", and it is scoped to the record generation that decided it.
    """
    if generation is None:
        generation = (await (await db_conn.execute(
            "SELECT active_record_generation FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    entity_id = str(uuid.uuid4())
    await db_conn.execute(
        "INSERT INTO entity (id, novel_id, record_generation_id, kind, canonical, first_seen_chapter) "
        "VALUES (%s, %s, %s, 'sect', 'Azure Cloud Sect', %s)",
        (entity_id, novel, generation, entity_chapter),
    )
    await db_conn.execute(
        "INSERT INTO alias (entity_id, surface, lang, first_seen_chapter, record_generation_id) "
        "VALUES (%s, '青云宗', 'zh', %s, %s)",
        (entity_id, entity_chapter, generation),
    )
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, entity_id, locked_at_chapter) "
        "VALUES (%s, '青云宗', 'Azure Cloud Sect', %s, %s)",
        (novel, entity_id, locked_at),
    )
    return entity_id


async def test_translated_novel_scans_the_translated_text_against_the_glossary(db_conn, novel):
    entity_id = await _link_term(db_conn, novel)

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


async def test_glossary_links_are_knowledge_time_gated(db_conn, novel):
    """A term locked at chapter 10 still renders at chapter 9 -- but unlinked.

    The identity behind it was learned at chapter 10, so naming it to a chapter-9 reader
    would be a spoiler in the §0 sense: the gate is knowledge-time, and it applies to who
    a name refers to just as much as to what happened.
    """
    await db_conn.execute(
        "INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) "
        "VALUES (%s,9,%s,'raw/early.txt','{}','done')",
        (novel, f"sha256:{novel}:early"),
    )
    entity_id = await _link_term(db_conn, novel, entity_chapter=10, locked_at=9)
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
    entity_id = await _link_term(db_conn, novel)
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    ctx.provider.response = lambda _prompt, system: (
        '{"alignments":[]}' if "Map each offered" in system
        else '{"names": ["Azure Cloud Sect", "Ling Feng"]}')
    state = _state(source_lang="zh", translation="Ling Feng joined Azure Cloud Sect.")
    await DisplayScanStage().run(ctx, state)
    assert [(s.alias_id, state.translation[s.char_start:s.char_end]) for s in state.display_spans] == [
        ("", "Ling Feng"), (entity_id, "Azure Cloud Sect")]


async def test_links_follow_the_active_record_generation_only(db_conn, novel):
    """Identity belongs to the generation that decided it.

    A retired generation's entity is not an authority over today's display: after a
    rebuild the old entity ids may name different characters entirely, so a span must
    fall back to an unlinked placeholder rather than carry a stale id forward.
    """
    retired = (await (await db_conn.execute(
        """INSERT INTO record_generation (novel_id, ontology, prompt_version, checks_version,
             extraction_model, source_lang, target_lang, state, retired_at)
           VALUES (%s, %s, 'v0', 'v0', 'test-model', 'zh', 'en', 'retired', now())
           RETURNING id""", (novel, json.dumps(ONTOLOGY)))).fetchone())[0]
    active_entity = await _link_term(db_conn, novel)
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")

    async def scan() -> str:
        state = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.")
        await DisplayScanStage().run(ctx, state)
        [span] = state.display_spans
        return span.alias_id

    assert await scan() == active_entity

    # Point the novel at the retired generation: its identity has no entity of its own
    # for this surface, so the term still renders but names nobody.
    current = (await (await db_conn.execute(
        "SELECT active_record_generation FROM novel WHERE id=%s", (novel,))).fetchone())[0]
    await db_conn.execute(
        "UPDATE novel SET active_record_generation=%s WHERE id=%s", (retired, novel))
    assert await scan() == ""

    await db_conn.execute(
        "UPDATE novel SET active_record_generation=%s WHERE id=%s", (current, novel))
    assert await scan() == active_entity


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
