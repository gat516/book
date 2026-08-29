"""Phase 1.7 integration tests: Postgres + in-memory provider/object storage."""

from __future__ import annotations

import io
import json

import pytest

from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel
from pipeline.batch import BatchManager
from pipeline.cache import LLMCache
from pipeline.context import NovelMeta, PipelineState, StageContext, language_profile_for
from pipeline.envelope import ChapterEnvelope, SourceMeta
from pipeline.stages.translate import TranslateStage

pytestmark = pytest.mark.db

CHAPTER = 7
RAW_HASH = "sha256:translation-test"
SOURCE = "青云宗的大门打开了。"
TRANSLATION = "The gates of the Azure Cloud Sect opened."
ONTOLOGY = {"kinds": ["sect"], "attributes": [], "relations": []}


class _Response(io.BytesIO):
    def release_conn(self) -> None:
        pass


class FakeObjects:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], bytes] = {}

    def put_object(self, bucket, key, body, *, length, content_type):
        value = body.read(length)
        self.data[(bucket, key)] = value

    def get_object(self, bucket, key):
        return _Response(self.data[(bucket, key)])


def _state(novel_id: str) -> PipelineState:
    return PipelineState(
        envelope=ChapterEnvelope(
            novel_id=novel_id,
            chapter_index=CHAPTER,
            raw_text=SOURCE,
            source_lang="zh",
            source_meta=SourceMeta(raw_hash=RAW_HASH),
        )
    )


async def test_translation_pins_served_snapshot_and_rerun_reads_object(db_conn):
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        await db_conn.execute(
            """
            INSERT INTO chapter
              (novel_id, chapter_index, raw_hash, raw_uri, source_meta)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (novel_id, CHAPTER, RAW_HASH, "raw/test.txt", json.dumps({})),
        )
        await db_conn.execute(
            """
            INSERT INTO glossary
              (novel_id, source_term, target_term, version, locked_at_chapter)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (novel_id, "青云宗", "Azure Cloud Sect", 1, 1),
        )

        provider = FakeProvider(TRANSLATION, served_model="qwen3:8b-snapshot")
        objects = FakeObjects()
        cfg = make_config(llm_model_translate="qwen3:8b")
        ctx = StageContext(
            novel=NovelMeta(
                id=novel_id,
                source_lang="zh",
                target_lang="en",
                ontology=ONTOLOGY,
            ),
            language_profile=language_profile_for("zh"),
            provider=provider,
            batch_manager=BatchManager(provider),
            embed_provider=provider,
            db=db_conn,
            objects=objects,
            cfg=cfg,
            cache=LLMCache(FakeRedis()),
        )

        first = _state(novel_id)
        await TranslateStage().run(ctx, first)

        pin = await (
            await db_conn.execute(
                "SELECT translation_provider FROM novel WHERE id = %s", (novel_id,)
            )
        ).fetchone()
        chapter = await (
            await db_conn.execute(
                "SELECT translated_uri, translated_by, glossary_version FROM chapter "
                "WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, CHAPTER),
            )
        ).fetchone()

        assert provider.calls[0]["pin_model"] is True
        assert provider.calls[0]["model"] == "qwen3:8b"
        assert len(provider.batch_requests) == 1
        assert len(provider.batch_polls) == 1
        assert pin == ("ollama:qwen3:8b-snapshot",)
        assert chapter[1:] == ("ollama:qwen3:8b-snapshot", 1)
        assert objects.data[(cfg.object_bucket, chapter[0])].decode() == TRANSLATION
        assert [chunk.text for chunk in first.chunks] == [TRANSLATION]

        second = _state(novel_id)
        await TranslateStage().run(ctx, second)
        assert len(provider.calls) == 1
        assert second.translation == TRANSLATION
    finally:
        await delete_novel(db_conn, novel_id)


async def test_per_novel_provider_pin_ignores_process_default(db_conn):
    """PLAN.md Phase N4 regression: a novel pinned to a provider that differs from the
    process's LLM_PROVIDER must not trip the pin-mismatch check, as long as
    ctx.provider_id reflects the novel's own resolved provider — which is exactly what
    worker.py's _provider_for_novel sets it to from novel_provider_config, not what
    ctx.cfg.llm_provider (the process-wide fallback) says. Before this phase's fix, both
    spots in _run_with_pin compared against ctx.cfg.llm_provider directly and this
    novel/process combination would have raised."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        await db_conn.execute(
            "UPDATE novel SET translation_provider = %s WHERE id = %s",
            ("deepseek:deepseek-chat", novel_id),
        )
        await db_conn.execute(
            """
            INSERT INTO chapter
              (novel_id, chapter_index, raw_hash, raw_uri, source_meta)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (novel_id, CHAPTER, RAW_HASH, "raw/test.txt", json.dumps({})),
        )

        provider = FakeProvider(TRANSLATION, provider="deepseek", served_model="deepseek-chat")
        objects = FakeObjects()
        # Process-wide default is ollama; this novel's own resolved provider (as the
        # worker would set ctx.provider_id from its novel_provider_config row) is deepseek.
        cfg = make_config(llm_provider="ollama", llm_model_translate="deepseek-chat")
        ctx = StageContext(
            novel=NovelMeta(id=novel_id, source_lang="zh", target_lang="en", ontology=ONTOLOGY),
            language_profile=language_profile_for("zh"),
            provider=provider,
            batch_manager=BatchManager(provider),
            embed_provider=provider,
            db=db_conn,
            objects=objects,
            cfg=cfg,
            cache=LLMCache(FakeRedis()),
            provider_id="deepseek",
        )

        state = _state(novel_id)
        await TranslateStage().run(ctx, state)  # must not raise

        chapter = await (
            await db_conn.execute(
                "SELECT translated_by FROM chapter WHERE novel_id = %s AND chapter_index = %s",
                (novel_id, CHAPTER),
            )
        ).fetchone()
        assert chapter == ("deepseek:deepseek-chat",)
    finally:
        await delete_novel(db_conn, novel_id)


async def test_bootstrapped_chapter_skips_translation_entirely(db_conn):
    """PLAN.md N5/N6: a chapter scraped from an already-translated site (or pasted with
    an explicit translated_text) has translated_uri set by ingest-api at insert time,
    translated_by='external', and no translate job ever created. This must be a distinct
    path from the ordinary job_is_done cache-hit above — that one requires a *completed*
    job row, which a bootstrapped chapter never has."""
    novel_id = await make_novel(
        db_conn, source_lang="zh", target_lang="en", ontology=json.dumps(ONTOLOGY)
    )
    try:
        objects = FakeObjects()
        objects.data[("bucket", "translated/bootstrap.txt")] = TRANSLATION.encode()
        await db_conn.execute(
            """
            INSERT INTO chapter
              (novel_id, chapter_index, raw_hash, raw_uri, source_meta,
               translated_uri, translated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (novel_id, CHAPTER, RAW_HASH, "raw/test.txt", json.dumps({}),
             "translated/bootstrap.txt", "external"),
        )

        provider = FakeProvider("should never be called")
        cfg = make_config(object_bucket="bucket")
        ctx = StageContext(
            novel=NovelMeta(id=novel_id, source_lang="zh", target_lang="en", ontology=ONTOLOGY),
            language_profile=language_profile_for("zh"),
            provider=provider,
            batch_manager=BatchManager(provider),
            embed_provider=provider,
            db=db_conn,
            objects=objects,
            cfg=cfg,
            cache=LLMCache(FakeRedis()),
        )

        state = _state(novel_id)
        await TranslateStage().run(ctx, state)

        assert state.translation == TRANSLATION
        assert [chunk.text for chunk in state.chunks] == [TRANSLATION]
        assert provider.calls == []
        assert provider.batch_requests == []

        job_count = await (
            await db_conn.execute(
                "SELECT count(*) FROM job WHERE novel_id = %s AND stage = 'translate'",
                (novel_id,),
            )
        ).fetchone()
        assert job_count == (0,)
    finally:
        await delete_novel(db_conn, novel_id)


async def test_deleted_glossary_terms_are_not_constraints_but_advance_cache_version(db_conn):
    from pipeline.stages.translate import _glossary
    novel_id = await make_novel(db_conn)
    try:
        await db_conn.execute(
            "INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter, deleted) "
            "VALUES (%s, 'gone', 'Removed', 5, 0, true)", (novel_id,),
        )
        assert await _glossary(db_conn, novel_id) == (5, [])
        await db_conn.execute(
            "INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter) "
            "VALUES (%s, 'kept', 'Retained', 4, 0)", (novel_id,),
        )
        assert await _glossary(db_conn, novel_id) == (5, [('kept', 'Retained', 'semantic_term')])
    finally:
        await delete_novel(db_conn, novel_id)


async def _run_protected_case(db_conn,retry_output: str):
    novel_id=await make_novel(db_conn,source_lang="zh",target_lang="en",ontology=json.dumps(ONTOLOGY))
    await db_conn.execute("""INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta)
        VALUES(%s,%s,%s,'raw/test.txt','{}')""",(novel_id,CHAPTER,RAW_HASH))
    await db_conn.execute("""INSERT INTO glossary(novel_id,source_term,target_term,version,locked_at_chapter)
        VALUES(%s,'青云宗','Azure Cloud Sect',1,1)""",(novel_id,))
    def respond(prompt,system):
        return retry_output if '<locked-term' in prompt else 'The gates opened clearly.'
    provider=FakeProvider(respond)
    objects=FakeObjects();redis=FakeRedis();cfg=make_config()
    ctx=StageContext(novel=NovelMeta(id=novel_id,source_lang="zh",target_lang="en",ontology=ONTOLOGY),
        language_profile=language_profile_for("zh"),provider=provider,batch_manager=BatchManager(provider),
        embed_provider=provider,db=db_conn,objects=objects,cfg=cfg,cache=LLMCache(redis))
    state=_state(novel_id)
    await TranslateStage().run(ctx,state)
    row=await (await db_conn.execute("""SELECT translated_uri,translation_warning_code,translation_warning_count
        FROM chapter WHERE novel_id=%s AND chapter_index=%s""",(novel_id,CHAPTER))).fetchone()
    return novel_id,state,row,objects,redis,provider


async def test_protected_retry_succeeds_and_saves_clean_text(db_conn):
    novel_id=None
    try:
        novel_id,state,row,objects,redis,provider=await _run_protected_case(
            db_conn,'The gates of <locked-term data-id="t0">Azure Cloud Sect</locked-term> opened.')
        assert state.translation=='The gates of Azure Cloud Sect opened.'
        assert row[1:]==(None,0) and len(provider.calls)==2
        assert objects.data[(make_config().object_bucket,row[0])].decode()==state.translation
        assert redis.store,"valid protected output remains content-cacheable"
    finally:
        if novel_id: await delete_novel(db_conn,novel_id)


async def test_nonempty_original_is_saved_with_warning_when_retry_still_misses(db_conn):
    novel_id=None
    try:
        novel_id,state,row,objects,redis,provider=await _run_protected_case(
            db_conn,'The sect gates opened, still without the locked wording.')
        assert state.translation=='The gates opened clearly.'
        assert row[1:]==('locked_terms_missing',1) and len(provider.calls)==2
        assert objects.data[(make_config().object_bucket,row[0])].decode()==state.translation
        assert redis.store=={},"warning output must never enter the response cache"
    finally:
        if novel_id: await delete_novel(db_conn,novel_id)
