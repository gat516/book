"""DISPLAY SCAN (instructions.md §5 step 6; PLAN.md Phase 5.2).

DISPLAY SCAN finds LOCKED GLOSSARY TERMS in the displayed text: the translation, or the
source itself for a same-language novel. Spans are terminology only and never carry an
entity id.

Needs a live Postgres for the glossary query — skipped cleanly via db_conn when
unreachable, same as test_resolve.py.
"""

from __future__ import annotations

import json

import pytest
from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel

from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
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


async def _lock_term(db_conn, novel, *, locked_at: int = 1, source: str = "青云宗",
                     target: str = "Azure Cloud Sect") -> None:
    await db_conn.execute(
        "INSERT INTO glossary (novel_id, source_term, target_term, locked_at_chapter) "
        "VALUES (%s, %s, %s, %s)",
        (novel, source, target, locked_at),
    )


async def test_translated_novel_scans_the_translated_text_against_the_glossary(db_conn, novel):
    await _lock_term(db_conn, novel)

    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    translated = "He returned to the Azure Cloud Sect."
    state = _state(source_lang="zh", translation=translated)

    await DisplayScanStage().run(ctx, state)

    [span] = state.display_spans
    assert span.alias_id == ""
    assert translated[span.char_start : span.char_end] == "Azure Cloud Sect"


async def test_glossary_terms_are_knowledge_time_gated(db_conn, novel):
    """A term locked at chapter 10 is not a searchable surface for chapter 9."""
    await _lock_term(db_conn, novel, locked_at=10)
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")

    early = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.",
                   chapter_index=9)
    await DisplayScanStage().run(ctx, early)
    assert [s for s in early.display_spans if s.char_start == 19] == []

    eligible = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.",
                      chapter_index=12)
    await DisplayScanStage().run(ctx, eligible)
    [span] = eligible.display_spans
    assert eligible.translation[span.char_start:span.char_end] == "Azure Cloud Sect"


async def test_translate_skipped_leaves_no_display_spans(db_conn, novel):
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    state = _state(source_lang="zh", translation=None)

    await DisplayScanStage().run(ctx, state)

    assert state.display_spans == []


async def test_untranslated_novel_scans_its_source_text(db_conn, novel):
    await _lock_term(db_conn, novel, source="青云宗", target="青云宗")
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="zh")
    state = _state(source_lang="zh")

    await DisplayScanStage().run(ctx, state)

    [span] = state.display_spans
    assert span.alias_id == ""
    assert state.envelope.raw_text[span.char_start:span.char_end] == "青云宗"


async def test_discovered_names_publish_before_facts(db_conn, novel):
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
        "SELECT char_start, char_end FROM mention_span WHERE novel_id=%s ORDER BY char_start",
        (novel,),
    )).fetchall()
    assert rows == [(0, 9), (22, 33)]
    aligned = await (await db_conn.execute(
        "SELECT source_term,display_term,char_start,char_end FROM term_rendering_occurrence "
        "WHERE novel_id=%s ORDER BY char_start", (novel,))).fetchall()
    assert aligned == [("他", "Ling Feng", 0, 9), ("青云宗", "Black Tower", 22, 33)]
    assert len(ctx.provider.calls) == 2  # one discovery + one alignment; rerun is cached


async def test_primed_names_are_found_by_exact_search_without_a_model_call(db_conn, novel):
    from pipeline.display_names import TermRenderingOccurrence
    from pipeline.term_choices import record_term_choices
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    state = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.")
    # What TRANSLATE records from the source-names pass before translating.
    await record_term_choices(ctx, state, [TermRenderingOccurrence(
        "青云宗", "Azure Cloud Sect", 4, 7, method="source_names", term_role="semantic_term")])
    state.source_names_primed = True
    await DisplayScanStage().run(ctx, state)
    rows = await (await db_conn.execute(
        "SELECT char_start, char_end FROM mention_span WHERE novel_id=%s", (novel,))).fetchall()
    assert rows == [(19, 35)]
    assert ctx.provider.calls == []  # no discovery, no alignment


async def test_discovery_keeps_glossary_spans_and_links_no_names(db_conn, novel):
    await _lock_term(db_conn, novel)
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    ctx.provider.response = lambda _prompt, system: (
        '{"alignments":[]}' if "Map each offered" in system
        else '{"names": ["Azure Cloud Sect", "Ling Feng"]}')
    state = _state(source_lang="zh", translation="Ling Feng joined Azure Cloud Sect.")
    await DisplayScanStage().run(ctx, state)
    assert [(s.alias_id, state.translation[s.char_start:s.char_end]) for s in state.display_spans] == [
        ("", "Ling Feng"), ("", "Azure Cloud Sect")]


async def test_mentions_are_chapter_gated_by_rls(db_conn, novel):
    await db_conn.execute(
        "INSERT INTO mention_span(novel_id,chapter_index,char_start,char_end) VALUES (%s,12,0,3)",
        (novel,),
    )
    for at, expected in [(11, []), (12, [(0,)])]:
        async with db_conn.transaction():
            await db_conn.execute("SET LOCAL ROLE rls_reader")
            await db_conn.execute("SELECT set_config('app.novel_id', %s, true), set_config('app.current_chapter', %s, true)", (novel, str(at)))
            rows = await (await db_conn.execute("SELECT char_start FROM mention_span WHERE novel_id=%s", (novel,))).fetchall()
            assert rows == expected


async def test_two_terms_with_one_spelling_make_one_span(db_conn, novel):
    """A variant pair (東皇鐘 / 東皇鍾) can share an English spelling. The span is written
    once -- a second one at the same offsets broke the chapter on every retry."""
    await _lock_term(db_conn, novel, source="青云宗", target="Azure Cloud Sect")
    await db_conn.execute("""INSERT INTO character_name_review
        (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason,term_role)
        VALUES(%s,'青雲宗',1,'h',0,1,'q','[{"target_term": "Azure Cloud Sect"}]','test','semantic_term')""", (novel,))
    ctx = _ctx(db_conn, novel, source_lang="zh", target_lang="en")
    state = _state(source_lang="zh", translation="He returned to the Azure Cloud Sect.")
    state.envelope = state.envelope.model_copy(update={"raw_text": "他回到了青云宗和青雲宗。"})
    state.source_names_primed = True

    await DisplayScanStage().run(ctx, state)

    [span] = state.display_spans
    [rendering] = state.term_renderings
    assert rendering.source_term == "青云宗"  # the locked term keeps it
    rows = await (await db_conn.execute(
        "SELECT count(*) FROM term_rendering_occurrence WHERE novel_id=%s", (novel,))).fetchone()
    assert rows == (1,)
