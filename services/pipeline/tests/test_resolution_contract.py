from pipeline.resolution import build_disambiguation_system_prompt


def test_same_language_resolution_does_not_request_a_generated_name():
    prompt = build_disambiguation_system_prompt(source_lang="en", target_lang="en")
    assert "target_term" not in prompt


def test_translated_resolution_names_the_actual_language_pair():
    prompt = build_disambiguation_system_prompt(source_lang="zh", target_lang="en")
    assert "translated from zh to en" in prompt
    assert '"target_term"' in prompt
