from pipeline.envelope import QueueMessage
from pipeline.translation import respell_names


def test_swaps_every_whole_name_occurrence():
    text = "Dragon Fei nodded. Later, Dragon Fei left."
    assert respell_names(text, [("Dragon Fei", "Long Fei")], [], "en") == \
        "Long Fei nodded. Later, Long Fei left."


def test_a_longer_name_containing_the_old_spelling_is_left_alone():
    text = "Lord Long Fei bowed to Long Fei."
    assert respell_names(text, [("Long Fei", "Fei Long")], ["Lord Long Fei"], "en") == \
        "Lord Long Fei bowed to Fei Long."


def test_part_of_a_word_is_not_a_name():
    assert respell_names("Anna met Ann.", [("Ann", "Anne")], [], "en") == "Anna met Anne."


def test_absent_old_spelling_asks_for_a_retranslation():
    assert respell_names("Ling Feng left.", [("Dragon Fei", "Long Fei")], [], "en") is None


def test_unchanged_spelling_is_a_no_op():
    assert respell_names("Long Fei left.", [("Long Fei", "Long Fei")], [], "en") == "Long Fei left."


def test_queue_message_reads_the_go_field_names():
    msg = QueueMessage.model_validate_json(
        '{"novel_id":"n","chapter_index":1,"retranslate":true,'
        '"respell":[{"from":"Dragon Fei","to":"Long Fei"}]}')
    assert [(r.old, r.new) for r in msg.respell] == [("Dragon Fei", "Long Fei")]
