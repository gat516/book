from pipeline.context import language_profile_for
from pipeline.jobs import idempotency_key
from pipeline.stages.chunk import chunk_text
import pytest

from pipeline.translation import (
    GlossaryViolation,
    build_system_prompt,
    build_user_prompt,
    prime_glossary_terms,
    protect_glossary_terms,
    strip_locked_term_tags,
    validate_glossary_constraints,
)
from pipeline.stages.translate import _translation_fingerprint
from types import SimpleNamespace

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


def test_translation_fingerprint_uses_complete_glossary_not_only_version():
    ctx=SimpleNamespace(novel=SimpleNamespace(source_lang="zh",target_lang="en",ontology={"kinds":["place"]}))
    assert _translation_fingerprint(ctx,[("九神殿","Dream Palace")]) != _translation_fingerprint(ctx,[("九神殿","Nine Gods Hall")])


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


def test_glossary_validation_allows_pronouns_for_repeated_mentions():
    """At-least-once, not occurrence parity.

    The source names the sect twice; good English names it once and falls back to a
    pronoun. Demanding parity failed correct translations for doing the idiomatic thing,
    and could not be satisfied anyway once the locked target was wrong — the chapter that
    motivated priming had a 3B model score 0/3 on every retry at every temperature.
    """
    validate_glossary_constraints(
        "青云宗与青云宗结盟。",
        "The Azure Cloud Sect formed an alliance with it.",
        [("青云宗", "Azure Cloud Sect")],
    )


def test_glossary_validation_still_rejects_a_dropped_term():
    with pytest.raises(GlossaryViolation, match="Azure Cloud Sect"):
        validate_glossary_constraints(
            "青云宗与青云宗结盟。",
            "The two sects formed an alliance.",
            [("青云宗", "Azure Cloud Sect")],
        )


def test_priming_substitutes_locked_terms_into_the_source():
    primed = prime_glossary_terms("青云宗的大门打开了。", [("青云宗", "Azure Cloud Sect")])
    assert primed == "Azure Cloud Sect的大门打开了。"
    assert "青云宗" not in primed


def test_priming_prefers_the_longest_locked_term():
    """A shorter term must not consume the prefix of a longer one."""
    primed = prime_glossary_terms(
        "九神殿主走进九神殿。",
        [("九神殿", "Nine Gods Hall"), ("九神殿主", "Lord of the Nine Gods Hall")],
    )
    assert primed == "Lord of the Nine Gods Hall走进Nine Gods Hall。"


def test_priming_does_not_rescan_injected_targets():
    """One pass only: a target containing another source term is left alone.

    Without this the second rule would rewrite the "Hall" injected by the first,
    corrupting a term the glossary had already settled.
    """
    primed = prime_glossary_terms("神殿。", [("神殿", "Hall of 神"), ("神", "God")])
    assert primed == "Hall of 神。"


def test_priming_ignores_blank_terms_and_empty_glossary():
    assert prime_glossary_terms("李逍遥笑了。", []) == "李逍遥笑了。"
    assert prime_glossary_terms("李逍遥笑了。", [("", "X"), ("  ", "Y")]) == "李逍遥笑了。"


def test_protected_retry_tags_are_intact_and_stripped_without_leaking_markup():
    protected=protect_glossary_terms("九神殿开门。",[("九神殿","Dream Palace")])
    assert protected=='<locked-term data-id="t0">Dream Palace</locked-term>开门。'
    assert strip_locked_term_tags(protected)=="Dream Palace开门。"
    with pytest.raises(GlossaryViolation,match="malformed"):
        strip_locked_term_tags('<locked-term data-id="t0">Dream Palace')


def test_primed_chapter_satisfies_validation_against_the_original_source():
    """The end-to-end contract: priming is what makes the backstop pass.

    Validation is checked against the ORIGINAL source, so priming has to survive a model
    that merely copies the injected form through.
    """
    source = "青云宗与青云宗结盟。"
    primed = prime_glossary_terms(source, [("青云宗", "Azure Cloud Sect")])
    translated = primed.replace("与", " allied with ").replace("结盟。", ".")
    validate_glossary_constraints(source, translated, [("青云宗", "Azure Cloud Sect")])


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
