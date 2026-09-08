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
        await admin.execute("INSERT INTO novel (id, title, source_lang, target_lang, ontology) VALUES (%s, 'Ask test', 'en', 'en', %s::jsonb), (%s, 'Other', 'en', 'en', '{}')", (novel_id, '{"kinds":["character"],"attributes":[{"name":"title","kinds":["character"]}],"relations":["ally"]}', other_novel_id))
        await admin.execute("INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter, embedding) VALUES (%s, %s, 'character', 'Hero', 1, %s::vector), (%s, %s, 'character', 'Future', 500, %s::vector)", (hero_id, novel_id, vector(), future_id, novel_id, vector()))
        await admin.execute("INSERT INTO chunk (novel_id, chapter_index, text, embedding) VALUES (%s, 220, 'visible chunk', %s::vector), (%s, 500, 'future chunk', %s::vector), (%s, 1, 'other novel', %s::vector)", (novel_id, vector(), novel_id, vector(), other_novel_id, vector()))
        await admin.execute("""INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,gloss,first_seen_chapter,admitted_at_chapter)
             VALUES (%s,'attribute','candidate_attr',ARRAY['character'],'candidate','',0,NULL),
                    (%s,'relation','banned_rel',ARRAY['character'],'banned','',0,NULL),
                    (%s,'attribute','looks',ARRAY['character'],'admitted','',0,0),
                    (%s,'relation','candidate_rel',ARRAY['character'],'candidate','',0,NULL),
                    (%s,'relation','friend',ARRAY['character'],'admitted','',0,0)""", (novel_id, novel_id, novel_id, novel_id, novel_id))
        await admin.execute("INSERT INTO novel_vocabulary_alias(novel_id,term_type,surface,name,known_from_chapter,created_by) VALUES (%s,'attribute','honorific','title',1,'test'), (%s,'attribute','future_title','title',500,'test'), (%s,'attribute','looks','appearance',1,'test'), (%s,'relation','friend','ally',1,'test')", (novel_id, novel_id, novel_id, novel_id))
        await admin.execute("""INSERT INTO fact (novel_id, entity_id, attribute, value, valid_from_chapter, source_chapter)
             VALUES (%s, %s, 'title', 'known', 1, 220),
                    (%s, %s, 'honorific', 'canonicalized', 1, 220),
                    (%s, %s, 'future_title', 'future alias', 1, 220),
                    (%s, %s, 'candidate_attr', 'candidate fact', 1, 220),
                    (%s, %s, 'appearance', 'old appearance', 1, 220),
                    (%s, %s, 'looks', 'canonical appearance', 1, 220),
                    (%s, %s, 'title', 'future revelation', 1, 500)""", (novel_id, hero_id, novel_id, hero_id, novel_id, hero_id, novel_id, hero_id, novel_id, hero_id, novel_id, hero_id, novel_id, hero_id))
        ally_edge = await admin.execute("INSERT INTO edge (novel_id, src_id, dst_id, rel_type, valid_from_chapter, source_chapter) VALUES (%s,%s,%s,'ally',1,220) RETURNING id", (novel_id, hero_id, hero_id))
        ally_edge_id = (await ally_edge.fetchone())[0]
        await admin.execute("INSERT INTO edge (novel_id, src_id, dst_id, rel_type, valid_from_chapter, source_chapter, supersedes) VALUES (%s,%s,%s,'candidate_rel',1,220,%s), (%s,%s,%s,'banned_rel',1,220,NULL), (%s,%s,%s,'friend',1,220,NULL)", (novel_id, hero_id, hero_id, ally_edge_id, novel_id, hero_id, hero_id, novel_id, hero_id, hero_id))
        try:
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as reader:
                await reader.execute("SET ROLE rls_reader")
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id', %s, true)", (novel_id,))
                    await reader.execute("SELECT set_config('app.current_chapter', '220', true)")
                    sources = await retrieve(reader, novel_id, 220, [1.0] + [0.0] * 767, max_chunks=8, max_entities=8, max_facts=64, max_edges=32)
                rendered = "\n".join(source.text for source in sources)
                assert "visible chunk" in rendered
                assert "known" not in rendered
                assert "canonicalized" in rendered and "Hero: title =" in rendered
                assert "Hero: appearance = canonical appearance" in rendered
                assert "Hero: looks" not in rendered
                assert "future alias" not in rendered
                assert "candidate fact" not in rendered
                assert "banned_rel" not in rendered
                assert "--ally-->" in rendered
                assert "friend" not in rendered
                unchanged = await admin.execute("SELECT count(*) FROM fact WHERE novel_id=%s", (novel_id,))
                assert (await unchanged.fetchone())[0] == 7
                assert "future chunk" not in rendered
                assert "future revelation" not in rendered
                assert "other novel" not in rendered

                # Quarantine removes graph material but retains authorized prose chunks.
                await admin.execute("UPDATE graph_revision SET trusted=false WHERE novel_id=%s",(novel_id,))
                async with reader.transaction():
                    await reader.execute("SELECT set_config('app.novel_id', %s, true)",(novel_id,))
                    await reader.execute("SELECT set_config('app.current_chapter','220',true)")
                    repaired=await retrieve(reader,novel_id,220,[1.0]+[0.0]*767,max_chunks=8,max_entities=8,max_facts=64,max_edges=32)
                    assert repaired and all(s.kind=='chunk' for s in repaired)

                async with reader.transaction():
                    count = await reader.execute("SELECT count(*) FROM chunk")
                    assert (await count.fetchone())[0] == 0
        finally:
            await admin.execute("DELETE FROM fact WHERE novel_id = %s", (novel_id,))
            await admin.execute("DELETE FROM chunk WHERE novel_id = %s", (novel_id,))
            await admin.execute("DELETE FROM entity WHERE novel_id = %s", (novel_id,))
            await admin.execute("UPDATE novel SET active_graph_revision=NULL WHERE id=%s",(novel_id,))
            await admin.execute("DELETE FROM graph_revision WHERE novel_id=%s",(novel_id,))
            await admin.execute("DELETE FROM novel WHERE id = %s", (novel_id,))
            await admin.execute("DELETE FROM chunk WHERE novel_id = %s", (other_novel_id,))
            await admin.execute("UPDATE novel SET active_graph_revision=NULL WHERE id=%s",(other_novel_id,))
            await admin.execute("DELETE FROM graph_revision WHERE novel_id=%s",(other_novel_id,))
            await admin.execute("DELETE FROM novel WHERE id = %s", (other_novel_id,))
