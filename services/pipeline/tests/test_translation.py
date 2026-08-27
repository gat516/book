from pipeline.context import language_profile_for
from pipeline.jobs import idempotency_key
from pipeline.stages.chunk import chunk_text
import pytest

from pipeline.translation import (
    GlossaryViolation,
    build_system_prompt,
    build_user_prompt,
    validate_glossary_constraints,
)

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


def test_glossary_validation_accepts_locked_target():
    validate_glossary_constraints(
        "青云宗的大门打开了。",
        "The gates of the Azure Cloud Sect opened.",
        [("青云宗", "Azure Cloud Sect")],
    )


def test_glossary_validation_rejects_missing_target():
    with pytest.raises(GlossaryViolation, match="Azure Cloud Sect"):
        validate_glossary_constraints(
            "青云宗的大门打开了。",
            "The gates of the Blue Cloud Sect opened.",
            [("青云宗", "Azure Cloud Sect")],
        )


def test_glossary_validation_requires_every_occurrence():
    with pytest.raises(GlossaryViolation, match="1/2"):
        validate_glossary_constraints(
            "青云宗与青云宗结盟。",
            "Azure Cloud Sect formed an alliance with the other sect.",
            [("青云宗", "Azure Cloud Sect")],
        )


def test_glossary_validation_rejects_empty_translation():
    with pytest.raises(GlossaryViolation, match="empty"):
        validate_glossary_constraints("无名之人。", "  ", [])


def test_glossary_validation_ignores_blank_terms():
    """A blank locked term must not be treated as a constraint.

    Regression guard for a real failure: str.count("") is len(text) + 1, so an empty
    source term silently "requires" its target ~2000 times per chapter and fails every
    translation of that novel forever, under any model. Glossary rows are immutable, so
    one blank row was unrecoverable without hand-editing the table.
    """
    validate_glossary_constraints(
        "青云宗的大门打开了。",
        "The gates of the Azure Cloud Sect opened.",
        [("", "Xuan Lu"), ("   ", "Dream King"), ("青云宗", "Azure Cloud Sect")],
    )


def test_glossary_validation_ignores_terms_absent_from_chapter():
    validate_glossary_constraints(
        "李逍遥笑了。",
        "Li Xiaoyao smiled.",
        [("青云宗", "Azure Cloud Sect")],
    )
