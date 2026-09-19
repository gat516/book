"""RLS regression coverage for Ask-AI retrieval; opt in with ASKAI_TEST_DATABASE_URL.

Each way the gate could leak is asserted here: a future chapter and another novel. The
reader connects as ``rls_reader`` while the seed runs as the
owner, so a passing assertion means both layers held -- the explicit predicates in
``retrieve`` and the policies underneath them (§0, §13.1).
"""

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
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute(
            "INSERT INTO novel (id, title, source_lang, target_lang, ontology) "
            "VALUES (%s, 'Ask test', 'en', 'en', '{}'), (%s, 'Other', 'en', 'en', '{}')",
            (novel_id, other_novel_id))
        try:
            await admin.execute(
                "INSERT INTO chunk (novel_id, chapter_index, text, embedding) "
                "VALUES (%s, 220, 'visible chunk', %s::vector), (%s, 500, 'future chunk', %s::vector), "
                "(%s, 1, 'other novel', %s::vector)",
                (novel_id, vector(), novel_id, vector(), other_novel_id, vector()))

            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as reader:
                await reader.execute("SET ROLE rls_reader")
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id', %s, true)", (novel_id,))
                    await reader.execute("SELECT set_config('app.current_chapter', '220', true)")
                    sources = await retrieve(reader, novel_id, 220, [1.0] + [0.0] * 767,
                                             question="hero", max_chunks=8)
                rendered = "\n".join(source.text for source in sources)
                assert "visible chunk" in rendered
                assert "future chunk" not in rendered
                assert "other novel" not in rendered

                # RLS alone, with no GUCs set, returns nothing at all.
                async with reader.transaction():
                    count = await reader.execute("SELECT count(*) FROM chunk")
                    assert (await count.fetchone())[0] == 0
        finally:
            await admin.execute("DELETE FROM novel WHERE id IN (%s, %s)", (novel_id, other_novel_id))


@pytest.mark.asyncio
async def test_vector_space_filter_preserves_chapter_and_novel_gates():
    database_url = os.getenv("ASKAI_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ASKAI_TEST_DATABASE_URL is not set")
    novel, other = str(uuid.uuid4()), str(uuid.uuid4())
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute("INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES (%s,'Embedding test','en','en','{}'),(%s,'Other','en','en','{}')", (novel, other))
        try:
            for book, chapter, text, space in [
                (novel, 1, "matching visible", "current"),
                (novel, 1, "different model same width", "old"),
                (novel, 1, "untagged legacy", None),
                (novel, 2, "matching future", "current"),
                (other, 1, "another book", "current"),
            ]:
                await admin.execute("INSERT INTO chunk(novel_id,chapter_index,text,embedding,embedding_space) VALUES (%s,%s,%s,%s::vector,%s)", (book, chapter, text, vector(), space))
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as reader:
                await reader.execute("SET ROLE rls_reader")
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id',%s,true)", (novel,))
                    await reader.execute("SELECT set_config('app.current_chapter','1',true)")
                    sources = await retrieve(reader, novel, 1, [1.] + [0.] * 767,
                                             embedding_space="current", max_chunks=10)
                    assert [s.text for s in sources if s.kind == "chunk"] == ["matching visible"]
                    # New settings are readable by Ask AI, but never carry secrets.
                    await reader.execute("SELECT provider,model FROM embedding_config")
        finally:
            await admin.execute("DELETE FROM novel WHERE id IN (%s,%s)", (novel, other))
