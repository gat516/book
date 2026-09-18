"""Single provisional choices reuse alignment, never a separate provider call."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fixtures import make_novel, delete_novel
from pipeline.display_names import TermRenderingOccurrence
from pipeline.term_choices import provisional_plan, record_term_choices
from pipeline.stages.translate import _glossary


@pytest.mark.parametrize("surface,display,role,target,method", [
    ("凌峰", "Lingfeng", "chinese_person", "Ling Feng", "pinyin"),
    ("龙飞", "Longfei", "chinese_person", "Long Fei", "pinyin"),
    ("龍飛", "Longfei", "chinese_person", "Long Fei", "pinyin"),
    ("水寒", "Water Cold", "chinese_person", "Shuihan", "pinyin"),
    ("契科夫", "Chekov", "foreign_person", "Chekhov", "restored_name"),
    ("白衣剑圣", "White-Robed Sword Saint", "personal_title", "White-Robed Sword Saint", "translated_title"),
    ("天庭", "Heavenly Court", "semantic_term", "Heavenly Court", "semantic_translation"),
])
def test_one_choice_keeps_existing_rendering_rules_without_autoapproval(surface, display, role, target, method):
    plan = provisional_plan(surface, display, role, "en")
    assert len(plan.candidates) == 1
    assert plan.candidates[0].target_term == target
    assert plan.rendering_method == method
    assert plan.auto_target is None


@pytest.mark.db
async def test_choice_survives_later_mentions_and_is_reused_from_its_own_chapter(db_conn):
    novel = await make_novel(db_conn)
    try:
        ctx = SimpleNamespace(db=db_conn, provider=AsyncMock(), novel=SimpleNamespace(
            id=novel, source_lang="zh", target_lang="en"))
        state = SimpleNamespace(envelope=SimpleNamespace(raw_text="凌峰来了。", chapter_index=1))
        occurrence = TermRenderingOccurrence("凌峰", "Lingfeng", 0, 8, term_role="chinese_person")
        await record_term_choices(ctx, state, [occurrence, occurrence])
        # Even another valid proposal for this same chapter cannot replace the choice.
        await record_term_choices(ctx, state, [TermRenderingOccurrence(
            "凌峰", "Changed", 0, 7, term_role="foreign_person")])
        row = await (await db_conn.execute(
            "SELECT status,candidates,term_role FROM character_name_review WHERE novel_id=%s", (novel,))).fetchone()
        assert row[0] == "pending"
        assert [c["target_term"] for c in row[1]] == ["Ling Feng"]
        assert row[2] == "chinese_person"
        # Recorded before translation, so its own chapter is primed with it too; a chapter
        # before it never sees it (§0: no name from the future).
        assert await _glossary(db_conn, novel, chapter=0) == (0, [])
        assert await _glossary(db_conn, novel, chapter=1) == (0, [("凌峰", "Ling Feng", "character_name")])
        assert await _glossary(db_conn, novel, chapter=2) == (0, [("凌峰", "Ling Feng", "character_name")])
        assert (await (await db_conn.execute("SELECT count(*) FROM glossary WHERE novel_id=%s", (novel,))).fetchone())[0] == 0
        ctx.provider.complete.assert_not_awaited()
        # Explicit glossary decisions always override provisional spelling, including
        # a tombstone which must not resurrect the earlier provisional constraint.
        await db_conn.execute("""INSERT INTO glossary(novel_id,source_term,target_term,version,locked_at_chapter,constraint_class)
            VALUES(%s,'凌峰','Ling Fong',1,1,'character_name')""", (novel,))
        assert await _glossary(db_conn, novel, chapter=2) == (1, [("凌峰", "Ling Fong", "character_name")])
        await db_conn.execute("UPDATE glossary SET deleted=true WHERE novel_id=%s", (novel,))
        assert await _glossary(db_conn, novel, chapter=2) == (1, [])
    finally:
        await delete_novel(db_conn, novel)
