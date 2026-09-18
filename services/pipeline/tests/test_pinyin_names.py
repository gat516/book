from pipeline.pinyin_names import plan_character_name
import pytest


def test_standard_character_names_are_code_derived():
    assert plan_character_name("凌峰").auto_target == "Ling Feng"
    assert plan_character_name("黄少天").auto_target == "Huang Shaotian"


@pytest.mark.parametrize("surface,target", [
    ("龍飛", "Long Fei"), ("龙飞", "Long Fei"),
    ("黃少天", "Huang Shaotian"), ("張無忌", "Zhang Wuji"),
    ("歐陽鋒", "Ouyang Feng"),
])
def test_surname_spacing_is_the_same_in_traditional_and_simplified_script(surface, target):
    plan = plan_character_name(surface)
    assert plan.candidates[0].target_term == target
    assert plan.candidates[0].segmentation == "surname+given"


def test_surname_less_name_is_joined_but_requires_review():
    plan = plan_character_name("水寒")
    assert [c.target_term for c in plan.candidates] == ["Shuihan"]
    assert plan.auto_target is None
    assert "ambiguous_name_structure" in plan.reason
    assert "Water Cold" not in {c.target_term for c in plan.candidates}


def test_polyphonic_name_requires_review():
    plan = plan_character_name("单乐")
    assert plan.auto_target is None
    assert "polyphonic" in plan.reason
    assert len(plan.candidates) > 1


def test_foreign_and_nickname_names_require_review():
    assert plan_character_name("诺顿·威尔森").reason == "foreign_or_mixed_name"
    nickname = plan_character_name("阿采")
    assert nickname.auto_target is None
    assert "nickname_or_honorific" in nickname.reason


def test_generic_title_is_not_a_name_candidate():
    plan = plan_character_name("掌柜")
    assert plan.reason == "generic_title"
    assert plan.candidates == ()
