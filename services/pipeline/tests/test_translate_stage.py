"""Phase 1.7 integration tests: Postgres + in-memory provider/object storage."""

from __future__ import annotations

import io
import json

import pytest

from fixtures import FakeProvider, FakeRedis, delete_novel, make_config, make_novel
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
