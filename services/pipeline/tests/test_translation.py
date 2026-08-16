from pipeline.context import language_profile_for
from pipeline.jobs import idempotency_key
from pipeline.stages.chunk import chunk_text
from pipeline.translation import build_system_prompt, build_user_prompt

from fixtures import make_config


def test_translation_prompt_front_loads_glossary_and_keeps_chapter_in_user_block():
    body = "青云宗的大门打开了。"
    system = build_system_prompt(
        source_lang="zh",
        target_lang="en",
        ontology={"kinds": ["sect"]},
        glossary=[("青云宗", "Azure Cloud Sect")],
    )

    assert system.index("Ontology:") < system.index("Locked glossary:")
    assert body not in system
    assert body in build_user_prompt(body)
    assert "青云宗 => Azure Cloud Sect" in system


def test_translate_key_changes_with_glossary_snapshot():
    cfg = make_config()
    assert idempotency_key("translate", "sha256:x", cfg, glossary_version=1) != idempotency_key(
        "translate", "sha256:x", cfg, glossary_version=2
    )


def test_translated_text_uses_target_language_chunk_profile():
    chunks = chunk_text("The Azure Cloud Sect opened its gates.", language_profile_for("en"))
    assert chunks[0].text == "The Azure Cloud Sect opened its gates."
