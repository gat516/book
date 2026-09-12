"""RLS regression coverage for Ask-AI retrieval; opt in with ASKAI_TEST_DATABASE_URL.

Retrieval moved from fact/edge to the records tables, so the gate has more ways to leak
and each one is asserted here: a future chapter, another novel, an unpublished run, and a
retired generation. The reader connects as ``rls_reader`` while the seed runs as the
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


async def _seed_row(admin, *, novel_id, generation, run_id, chapter, index, content,
                    entity_id=None, surface="Hero"):
    row_id = str(uuid.uuid4())
    await admin.execute(
        """INSERT INTO record_row (id, novel_id, generation_id, run_id, original_index,
             record_type, source_chapter, source_hash)
           VALUES (%s, %s, %s, %s, %s, 'EVENT', %s, %s)""",
        (row_id, novel_id, generation, run_id, index, chapter, f"hash-{chapter}"))
    await admin.execute(
        "INSERT INTO record_value (row_id, field_name, source_value) VALUES (%s, 'what', %s)",
        (row_id, content))
    if entity_id:
        await admin.execute(
            "INSERT INTO record_participant (row_id, ordinal, field_name, surface, entity_id) "
            "VALUES (%s, 0, 'actor', %s, %s)",
            (row_id, surface, entity_id))
    return row_id


async def _seed_run(admin, *, novel_id, generation, chapter, status, version):
    run_id = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO chapter (novel_id, chapter_index, raw_uri, raw_hash, source_meta) "
        "VALUES (%s, %s, 'raw://c', %s, '{}'::jsonb)",
        (novel_id, chapter, f"hash-{chapter}"))
    await admin.execute(
        """INSERT INTO record_run (id, novel_id, generation_id, chapter_index, source_hash,
             request_identity, extraction_model, status, publication_version, published_at)
           VALUES (%s, %s, %s, %s, %s, 'identity', 'test-model', %s, %s,
                   CASE WHEN %s='published' THEN now() END)""",
        (run_id, novel_id, generation, chapter, f"hash-{chapter}", status, version, status))
    return run_id


@pytest.mark.asyncio
async def test_retrieval_is_gated_by_rls_and_effective_chapter() -> None:
    database_url = os.getenv("ASKAI_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ASKAI_TEST_DATABASE_URL is not set")
    novel_id, other_novel_id = str(uuid.uuid4()), str(uuid.uuid4())
    hero_id = str(uuid.uuid4())
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute(
            "INSERT INTO novel (id, title, source_lang, target_lang, ontology) "
            "VALUES (%s, 'Ask test', 'en', 'en', %s::jsonb), (%s, 'Other', 'en', 'en', '{}')",
            (novel_id, '{"kinds":["character"]}', other_novel_id))
        generation = (await (await admin.execute(
            "SELECT active_record_generation FROM novel WHERE id=%s", (novel_id,))).fetchone())[0]
        retired = (await (await admin.execute(
            """INSERT INTO record_generation (novel_id, ontology, prompt_version, checks_version,
                 extraction_model, source_lang, target_lang, state, retired_at)
               VALUES (%s, '{"kinds":["character"]}'::jsonb, 'v0', 'v0', 'test-model', 'en', 'en',
                       'retired', now()) RETURNING id""", (novel_id,))).fetchone())[0]
        try:
            await admin.execute(
                "INSERT INTO entity (id, novel_id, record_generation_id, kind, canonical, "
                "first_seen_chapter, embedding) VALUES (%s, %s, %s, 'character', 'Hero', 1, %s::vector)",
                (hero_id, novel_id, generation, vector()))
            await admin.execute(
                "INSERT INTO chunk (novel_id, chapter_index, text, embedding) "
                "VALUES (%s, 220, 'visible chunk', %s::vector), (%s, 500, 'future chunk', %s::vector), "
                "(%s, 1, 'other novel', %s::vector)",
                (novel_id, vector(), novel_id, vector(), other_novel_id, vector()))

            visible = await _seed_run(admin, novel_id=novel_id, generation=generation,
                                      chapter=220, status="published", version=1)
            future = await _seed_run(admin, novel_id=novel_id, generation=generation,
                                     chapter=500, status="published", version=2)
            unpublished = await _seed_run(admin, novel_id=novel_id, generation=generation,
                                          chapter=221, status="processing", version=None)
            stale = await _seed_run(admin, novel_id=novel_id, generation=retired,
                                    chapter=219, status="published", version=1)

            await _seed_row(admin, novel_id=novel_id, generation=generation, run_id=visible,
                            chapter=220, index=0, content="hero drew the blade", entity_id=hero_id)
            await _seed_row(admin, novel_id=novel_id, generation=generation, run_id=future,
                            chapter=500, index=0, content="future revelation", entity_id=hero_id)
            await _seed_row(admin, novel_id=novel_id, generation=generation, run_id=unpublished,
                            chapter=221, index=0, content="unpublished draft", entity_id=hero_id)
            await _seed_row(admin, novel_id=novel_id, generation=retired, run_id=stale,
                            chapter=219, index=0, content="superseded extraction")

            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as reader:
                await reader.execute("SET ROLE rls_reader")
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id', %s, true)", (novel_id,))
                    await reader.execute("SELECT set_config('app.current_chapter', '220', true)")
                    sources = await retrieve(reader, novel_id, 220, [1.0] + [0.0] * 767,
                                             question="hero", max_chunks=8, max_entities=8,
                                             max_records=64)
                rendered = "\n".join(source.text for source in sources)
                assert "visible chunk" in rendered
                assert "hero drew the blade" in rendered
                assert "future chunk" not in rendered
                assert "future revelation" not in rendered
                assert "unpublished draft" not in rendered
                assert "superseded extraction" not in rendered
                assert "other novel" not in rendered

                # RLS alone, with no GUCs set, returns nothing at all.
                async with reader.transaction():
                    count = await reader.execute("SELECT count(*) FROM record_row")
                    assert (await count.fetchone())[0] == 0
                    count = await reader.execute("SELECT count(*) FROM chunk")
                    assert (await count.fetchone())[0] == 0
        finally:
            await admin.execute("DELETE FROM novel WHERE id IN (%s, %s)", (novel_id, other_novel_id))
