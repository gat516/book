import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fixtures import make_config
from pipeline.refresh_name_reviews import refresh_reviews
from pipeline.stages.character_names import _discover, _refresh_pending, _rendering_plan


def context(items):
    async def complete(prompt, **kwargs):
        passages = json.loads(prompt.split("\n", 1)[1])["passages"]
        names = [{"passage_id": passages[0]["id"], **item} for item in items]
        return SimpleNamespace(text=json.dumps({"reviewed": True, "names": names}))
    return SimpleNamespace(
        novel=SimpleNamespace(id="novel", target_lang="en"), cfg=make_config(),
        provider=SimpleNamespace(complete=AsyncMock(side_effect=complete)),
    )


def item(surface, rendering, targets=()):
    return dict(surface=surface, rendering=rendering, targets=list(targets),
                kind="not_character" if rendering == "not_character" else "character")


async def test_transcribed_name_offers_restorations_not_pinyin_and_requires_review():
    ctx = context([item("劳伦斯", "foreign_personal", ["Lawrence", "Laurence"])])
    plans = await _discover(ctx, "他看向气息微弱的劳伦斯。")
    plan = plans["劳伦斯"]
    assert [c.target_term for c in plan.candidates] == ["Lawrence", "Laurence"]
    assert plan.auto_target is None
    assert plan.candidates[0].as_dict() == {
        "target_term": "Lawrence", "pronunciation": [], "segmentation": "", "method": "restored_name"}


async def test_chinese_personal_name_ignores_semantic_suggestions():
    plans = await _discover(context([item("水寒", "chinese_personal", ["Water Cold"])]), "水寒来了。")
    assert [c.target_term for c in plans["水寒"].candidates] == ["Shuihan"]


def test_foreign_name_overrides_accidental_deterministic_surname_split():
    plan = _rendering_plan("马克", "foreign_personal", ["Mark"])
    assert plan.auto_target is None
    assert [c.target_term for c in plan.candidates] == ["Mark", "Marc"]
    assert _rendering_plan("凌峰", "chinese_personal", []).auto_target == "Ling Feng"


async def test_personal_titles_translate_but_organizations_are_not_character_names():
    ctx = context([item("白衣剑圣", "titled_person", ["White-Robed Sword Saint"]),
                   item("天庭", "not_character")])
    plans = await _discover(ctx, "白衣剑圣走进了天庭。")
    assert set(plans) == {"白衣剑圣"}
    assert plans["白衣剑圣"].candidates[0].method == "translated_title"
    assert plans["白衣剑圣"].auto_target is None


def test_generic_title_does_not_become_name_review():
    assert _rendering_plan("掌柜", "titled_person", ["Shopkeeper"]).reason == "generic_title"


async def test_nonliteral_proposal_cannot_create_name():
    plans = await _discover(context([item("劳伦斯", "foreign_personal", ["Lawrence"])]), "他走了。")
    assert plans == {}


@pytest.mark.parametrize("targets", [[""], [" Lawrence"], ["劳伦斯"], ["x" * 161], ["a\nb"], [1], ["a"] * 5])
async def test_invalid_targets_rejected(targets):
    with pytest.raises(ValueError, match="invalid rendering"):
        await _discover(context([item("劳伦斯", "foreign_personal", targets)]), "劳伦斯走了。")


async def test_uncertain_foreign_spelling_requires_custom_review():
    plan = (await _discover(context([item("未知·威尔森", "foreign_personal")]), "未知·威尔森说话了。"))["未知·威尔森"]
    assert not plan.candidates
    assert plan.auto_target is None


async def test_known_transcription_survives_model_misclassification():
    plan = (await _discover(context([item("劳伦斯", "chinese_personal")]), "劳伦斯来了。"))["劳伦斯"]
    assert [c.target_term for c in plan.candidates] == ["Lawrence", "Laurence"]
    assert plan.auto_target is None
    assert _rendering_plan("马克", "chinese_personal", []).auto_target is None


def test_conventional_restorations_are_target_language_specific():
    plan = _rendering_plan("劳伦斯", "foreign_personal", ["ローレンス"], "ja")
    assert [c.target_term for c in plan.candidates] == ["ローレンス"]


def test_complete_middle_dot_names_restore_components_without_identity_guessing():
    plan = _rendering_plan("诺顿·威尔森", "chinese_personal", [])
    assert [c.target_term for c in plan.candidates] == ["Norton Wilson"]
    assert plan.auto_target is None


async def test_refresh_is_guarded_by_pending_status_and_original_chapter():
    db = SimpleNamespace(execute=AsyncMock())
    plan = _rendering_plan("劳伦斯", "foreign_personal", ["Lawrence"])
    await _refresh_pending(db, "novel", "劳伦斯", plan, 1)
    sql, params = db.execute.call_args.args
    assert "status='pending'" in sql
    assert "first_seen_chapter=%s" in sql
    assert "selected_target" not in sql
    assert params[-3:] == ("novel", "劳伦斯", 1)


async def test_offline_refresh_only_uses_saved_quote_and_does_not_approve():
    ctx = context([item("劳伦斯", "foreign_personal", ["Lawrence"])])
    cursor = SimpleNamespace(fetchall=AsyncMock(return_value=[("劳伦斯", 1, "劳伦斯走了。")]))
    ctx.db = SimpleNamespace(execute=AsyncMock(return_value=cursor))
    results = await refresh_reviews(ctx, source_term="劳伦斯", apply=True)
    assert results[0]["candidates"][0]["target_term"] == "Lawrence"
    assert ctx.db.execute.call_count == 2
    prompt = ctx.provider.complete.call_args.args[0]
    assert "劳伦斯走了。" in prompt
    assert "status='pending'" in ctx.db.execute.call_args_list[0].args[0]
    assert "status='pending'" in ctx.db.execute.call_args_list[1].args[0]


async def test_offline_refresh_preview_does_not_write():
    ctx = context([item("劳伦斯", "foreign_personal", ["Lawrence"])])
    cursor = SimpleNamespace(fetchall=AsyncMock(return_value=[("劳伦斯", 1, "劳伦斯走了。")]))
    ctx.db = SimpleNamespace(execute=AsyncMock(return_value=cursor))
    await refresh_reviews(ctx)
    assert ctx.db.execute.call_count == 1


@pytest.mark.db
async def test_persisted_refresh_preserves_approval_and_original_evidence(db_conn):
    from fixtures import delete_novel, make_novel
    from pipeline.stages.character_names import _record_surface

    novel_id = await make_novel(db_conn)
    try:
        ctx = context([])
        ctx.db = db_conn
        ctx.novel.id = novel_id
        state = SimpleNamespace(envelope=SimpleNamespace(raw_text="劳伦斯走了。", chapter_index=1))
        pending_plan = _rendering_plan("劳伦斯", "foreign_personal", [])
        assert await _record_surface(ctx, state, "劳伦斯", pending_plan)
        row = await (await db_conn.execute("""SELECT status,candidates,quote FROM character_name_review
            WHERE novel_id=%s AND source_term='劳伦斯'""", (novel_id,))).fetchone()
        assert row[0] == "pending"
        assert row[1][0]["target_term"] == "Lawrence"
        # Later evidence cannot replace the first-seen chapter's suggestions.
        later_plan = _rendering_plan("未知", "foreign_personal", ["Different"])
        await _refresh_pending(db_conn, novel_id, "劳伦斯", later_plan, 2)
        assert (await (await db_conn.execute("""SELECT candidates FROM character_name_review
            WHERE novel_id=%s AND source_term='劳伦斯'""", (novel_id,))).fetchone())[0] == row[1]
        await db_conn.execute("""UPDATE character_name_review SET status='approved',selected_target='Laurence'
            WHERE novel_id=%s AND source_term='劳伦斯'""", (novel_id,))
        await _refresh_pending(db_conn, novel_id, "劳伦斯", later_plan, 1)
        assert not await _record_surface(ctx, state, "劳伦斯", later_plan)
        approved = await (await db_conn.execute("""SELECT selected_target,candidates,quote
            FROM character_name_review WHERE novel_id=%s AND source_term='劳伦斯'""", (novel_id,))).fetchone()
        assert approved == ("Laurence", row[1], row[2])
        assert (await (await db_conn.execute("SELECT count(*) FROM glossary WHERE novel_id=%s",
                                            (novel_id,))).fetchone())[0] == 0
    finally:
        await delete_novel(db_conn, novel_id)
