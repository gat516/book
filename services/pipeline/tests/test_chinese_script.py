from pipeline.chinese_script import source_form

SOURCE = "他在神職司院見到了星蓮。"


def test_exact_term_is_returned_unchanged():
    assert source_form("星蓮", SOURCE) == "星蓮"


def test_other_script_is_mapped_to_the_sources_form():
    assert source_form("星莲", SOURCE) == "星蓮"          # simplified answer, traditional chapter
    assert source_form("神职司院", SOURCE) == "神職司院"
    assert source_form("星蓮", "他见到了星莲。") == "星莲"   # and the reverse


def test_a_different_character_is_still_rejected():
    assert source_form("燕", "琰走了。") is None           # same sound, different name
    assert source_form("不存在", SOURCE) is None
    assert source_form("", SOURCE) is None
