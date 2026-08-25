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
