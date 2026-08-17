"""RLS regression coverage for Ask-AI retrieval; opt in with ASKAI_TEST_DATABASE_URL."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from askai.retrieval import retrieve


def vector() -> str:
    return "[" + ",".join(["1"] + ["0"] * 767) + "]"


@pytest.mark.asyncio
async def test_retrieval_is_gated_by_rls_and_effective_chapter() -> None:
    database_url = os.getenv("ASKAI_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ASKAI_TEST_DATABASE_URL is not set")
    novel_id, other_novel_id = str(uuid.uuid4()), str(uuid.uuid4())
    hero_id, future_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute("INSERT INTO novel (id, title, source_lang, target_lang, ontology) VALUES (%s, 'Ask test', 'en', 'en', '{}'), (%s, 'Other', 'en', 'en', '{}')", (novel_id, other_novel_id))
        await admin.execute("INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter, embedding) VALUES (%s, %s, 'character', 'Hero', 1, %s::vector), (%s, %s, 'character', 'Future', 500, %s::vector)", (hero_id, novel_id, vector(), future_id, novel_id, vector()))
        await admin.execute("INSERT INTO chunk (novel_id, chapter_index, text, embedding) VALUES (%s, 220, 'visible chunk', %s::vector), (%s, 500, 'future chunk', %s::vector), (%s, 1, 'other novel', %s::vector)", (novel_id, vector(), vector(), other_novel_id, vector()))
        await admin.execute("INSERT INTO fact (novel_id, entity_id, attribute, value, valid_from_chapter, source_chapter) VALUES (%s, %s, 'title', 'known', 1, 220), (%s, %s, 'title', 'future revelation', 1, 500)", (novel_id, hero_id, novel_id, hero_id))
        try:
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=False) as reader:
                await reader.execute("SET ROLE rls_reader")
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id', %s, true)", (novel_id,))
                    await reader.execute("SELECT set_config('app.current_chapter', '220', true)")
                    sources = await retrieve(reader, novel_id, 220, [1.0] + [0.0] * 767, max_chunks=8, max_entities=8, max_facts=64, max_edges=32)
                rendered = "\n".join(source.text for source in sources)
                assert "visible chunk" in rendered
                assert "known" in rendered
                assert "future chunk" not in rendered
                assert "future revelation" not in rendered
                assert "other novel" not in rendered

                async with reader.transaction():
                    count = await reader.execute("SELECT count(*) FROM chunk")
                    assert (await count.fetchone())[0] == 0
        finally:
            await admin.execute("DELETE FROM fact WHERE novel_id = %s", (novel_id,))
            await admin.execute("DELETE FROM chunk WHERE novel_id = %s", (novel_id,))
            await admin.execute("DELETE FROM entity WHERE novel_id = %s", (novel_id,))
            await admin.execute("DELETE FROM novel WHERE id = %s", (novel_id,))
            await admin.execute("DELETE FROM chunk WHERE novel_id = %s", (other_novel_id,))
            await admin.execute("DELETE FROM novel WHERE id = %s", (other_novel_id,))
