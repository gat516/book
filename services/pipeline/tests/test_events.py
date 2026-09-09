from __future__ import annotations

import copy
import json

import pytest

from pipeline.events import (
    DEFAULT_EVENT_SCHEMA,
    EVENT_PROMPT_VERSION,
    EventEngine,
    EventProposals,
    canonical_summary,
    deduplicate_events,
    validate_events,
)
from pipeline.passages import PassageContract
from pipeline.passages import source_windows
from pipeline.event_rebuild import extraction_model, provider_connection, qualified, retry_delay_minutes


SOURCE = "阿瑞斯将三株星莲连根采摘，又把六纪星莲交给亚巴顿。"


def proposal(**overrides):
    participants = {
        "actor": None, "target": None, "instrument": None, "companion": None,
        "location": None, "item": "六纪星莲", "source": None, "giver": "阿瑞斯",
        "recipient": "亚巴顿", "subject": None,
    }
    row = {
        "event_type": "transfer",
        "action": "gave",
        "status": "completed",
        "participants": participants,
        "result": "Abaddon received the Six-Era Star Lotus.",
        "summary": "Ares gave the Six-Era Star Lotus to Abaddon.",
        "quote": SOURCE,
        "evidence_start": 0,
    }
    row.update(overrides)
    return row


def test_supported_event_survives_unresolved_identity():
    proposals = EventProposals(events=[proposal()])
    accepted, rejected = validate_events(
        SOURCE, proposals, DEFAULT_EVENT_SCHEMA,
        {"ares": {"surface": "阿瑞斯", "entity_id": "entity", "graph_revision_id": "revision"}},
    )
    assert rejected == []
    assert {(a["role"], a["surface"], a["mention_id"]) for a in accepted[0]["arguments"]} == {
        ("giver", "阿瑞斯", "ares"),
        ("recipient", "亚巴顿", None),
        ("item", "六纪星莲", None),
    }


def test_invented_and_empty_participants_fail_closed_individually():
    invented = {**proposal()["participants"], "giver": "不存在"}
    empty = {key: None for key in proposal()["participants"]}
    proposals = EventProposals(events=[
        proposal(participants=invented),
        proposal(participants=empty),
        proposal(),
    ])
    accepted, rejected = validate_events(SOURCE, proposals, DEFAULT_EVENT_SCHEMA, {})
    assert len(accepted) == 1
    assert [row["rejection"] for row in rejected] == [
        "event is missing required participant roles",
        "event has no schema-valid literal arguments",
    ]


def test_invalid_optional_argument_does_not_erase_supported_action():
    row = proposal(participants={
        **proposal()["participants"], "recipient": "inferred Abaddon",
    })
    accepted, rejected = validate_events(
        SOURCE, EventProposals(events=[row]), DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert rejected[0]["rejection"] == "event is missing required participant roles"


def test_completed_hypothetical_is_rejected():
    row = proposal(
        event_type="condition_change",
        participants={
            **{key: None for key in proposal()["participants"]}, "target": "阿瑞斯",
        },
        action="would have died",
        summary="Ares would have died if he were struck.",
    )
    accepted, rejected = validate_events(
        SOURCE, EventProposals(events=[row]), DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert rejected[0]["rejection"] == "completed event is hypothetical or conditional"


def test_participant_name_cannot_be_the_action():
    accepted, rejected = validate_events(
        SOURCE, EventProposals(events=[proposal(action="阿瑞斯")]), DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert rejected[0]["rejection"] == "event action is a participant rather than a verb phrase"


def test_deduplication_only_collapses_overlapping_same_participant_records():
    first = {"id": "event:0", **proposal()}
    first.pop("participants")
    first["arguments"] = [
        {"role": "giver", "surface": "阿瑞斯", "mention_id": None},
        {"role": "recipient", "surface": "亚巴顿", "mention_id": None},
        {"role": "item", "surface": "六纪星莲", "mention_id": None},
    ]
    duplicate = copy.deepcopy(first)
    duplicate.update(id="event:1", action="handed over")
    separate = copy.deepcopy(first)
    separate.update(id="event:2", event_type="acquisition",
                    arguments=[{"role": "actor", "surface": "阿瑞斯", "mention_id": None},
                               {"role": "item", "surface": "三株星莲", "mention_id": None}])
    assert [row["id"] for row in deduplicate_events([first, duplicate, separate])] == [
        "event:0", "event:2"
    ]


def test_attempted_and_prevented_are_distinct_from_completed():
    base = {"id": "event:0", **proposal()}
    base.pop("participants")
    base["arguments"] = [
        {"role": "giver", "surface": "阿瑞斯", "mention_id": None},
        {"role": "recipient", "surface": "亚巴顿", "mention_id": None},
        {"role": "item", "surface": "六纪星莲", "mention_id": None},
    ]
    attempted = {**copy.deepcopy(base), "id": "event:0", "status": "attempted"}
    prevented = {**copy.deepcopy(base), "id": "event:1", "status": "prevented"}
    completed = {**copy.deepcopy(base), "id": "event:2", "status": "completed"}
    # Status participates in the semantic identity, so an attempted acquisition can
    # never erase or masquerade as a completed one (§0.2).
    assert len(deduplicate_events([attempted, prevented, completed])) == 3


def test_event_passage_reference_is_materialized_from_offered_source():
    source = "Ares handed Abaddon the lotus."
    contract = PassageContract(source)
    passage_id = contract.passages[0]["id"]
    result = contract.materialize("propose", EventProposals, {"events": [{
        "event_type": "transfer",
        "action": "handed over",
        "status": "completed",
        "participants": {
            "actor": None, "target": None, "instrument": None, "companion": None,
            "location": None, "item": "lotus", "source": None, "giver": "Ares",
            "recipient": "Abaddon", "subject": None,
        },
        "result": "Abaddon received it",
        "summary": "Ares gave Abaddon the lotus.",
        "passage_id": passage_id,
    }]}, {})
    assert result.events[0].quote == source
    assert result.events[0].evidence_start == 0


def test_event_windows_keep_cross_paragraph_antecedent_as_exact_evidence():
    source = "Ares selected the lotus.\nHe handed it to Abaddon.\nThey departed."
    windows = source_windows(source, max_chars=48, overlap=12)
    handoff = next(window for window in windows if "handed" in window["text"])
    assert "Ares" in handoff["text"]
    assert source[handoff["char_start"]:handoff["char_end"]] == handoff["text"]


def test_event_materialization_preserves_leading_evidence_whitespace():
    source = " Earlier context. Ares selected it.\nHe handed the lotus to Abaddon."
    contract = PassageContract(source, max_chars=80, overlap=12, windows=True)
    selected = contract.passages[0]
    body = {"events": [{
        **proposal(), "participants": {
            **proposal()["participants"], "giver": "He", "recipient": "Abaddon",
            "item": "lotus",
        },
        "passage_id": selected["id"],
    }]}
    body["events"][0].pop("quote")
    body["events"][0].pop("evidence_start")
    event = contract.materialize("propose", EventProposals, body, {}).events[0]
    assert event.quote.startswith(" ")
    assert source[event.evidence_start:event.evidence_start + len(event.quote)] == event.quote


def test_event_activation_thresholds_are_fail_closed():
    metrics = {
        "reviewed_expected_events": 30,
        "reviewed_chapters": 5,
        "event_precision": 0.95,
        "plot_event_recall": 0.85,
        "argument_role_f1": 0.85,
        "completion_status_accuracy": 0.95,
        "critical_false_completions": 0,
        "evidence_valid": True,
        "reviewed": True,
        "publication_review_complete": True,
    }
    assert qualified(metrics)
    assert not qualified({**metrics, "critical_false_completions": 1})
    assert not qualified({**metrics, "plot_event_recall": 0.849})
    assert [retry_delay_minutes(attempt) for attempt in range(1, 6)] == [5, 15, 45, None, None]


def test_canonical_summary_cannot_carry_model_added_consequences():
    item = {
        "event_type": "acquisition", "action": "plucked",
        "arguments": [
            {"role": "actor", "surface": "Ares", "mention_id": None},
            {"role": "item", "surface": "three Star Lotus plants", "mention_id": None},
        ],
        "summary": "Ares plucked three plants and gave all of them away.",
    }
    assert canonical_summary(item) == "Ares plucked three Star Lotus plants."


def test_misplaced_transfer_roles_fail_closed():
    row = proposal(participants={
        **{key: None for key in proposal()["participants"]},
        "actor": "阿瑞斯", "target": "亚巴顿", "item": "六纪星莲",
    })
    accepted, rejected = validate_events(
        SOURCE, EventProposals(events=[row]), DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert rejected[0]["rejection"] == "event is missing required participant roles"


class _Cfg:
    """Only the fields EventEngine reads when a revision pins a hosted provider."""
    gemini_base_url = "https://example.invalid/v1beta/openai"
    gemini_api_key = "test-key"
    event_remote_timeout_seconds = 300.0


def _revision(provider, name="gemini-flash", identity=None):
    return {
        "id": "revision-1",
        "prompt_version": EVENT_PROMPT_VERSION,
        "event_schema": DEFAULT_EVENT_SCHEMA,
        "model": {"provider": provider, "name": name, "identity": identity or {}},
    }


def test_hosted_provider_revision_builds_without_local_ollama():
    engine = EventEngine(None, _Cfg(), _revision("gemini"))
    assert (engine.served_provider, engine.model) == ("gemini", "gemini-flash")
    # The streaming heartbeat and stream_sink handshake are Ollama-only; a hosted call
    # returns one whole body, so nothing may claim to observe partial output.
    assert engine.streams is False


def test_hosted_event_engine_uses_resolved_credentials(monkeypatch):
    captured = {}
    monkeypatch.setattr("pipeline.events.GeminiProvider",
                        lambda **kwargs: captured.update(kwargs) or object())
    EventEngine(None, _Cfg(), _revision("gemini"), provider_connection={
        "base_url": "https://saved.example/v1", "api_key": "saved-key",
    })
    assert captured["base_url"] == "https://saved.example/v1"
    assert captured["api_key"] == "saved-key"


@pytest.mark.asyncio
async def test_event_connection_takes_the_key_from_the_account_and_base_url_from_the_book(monkeypatch):
    """A book chooses where to call; only the account decides what to authenticate with.

    Before migration 0068 a book could carry its own key and it won here. That key was
    write-only (masked reads could not round-trip it, so the upsert COALESCEd it forward),
    which meant it outlived the provider it was entered for -- a book switched to another
    provider kept authenticating with the previous provider's secret. Dropping the column
    removes the ambiguity rather than papering over it.
    """
    from types import SimpleNamespace
    monkeypatch.setattr("pipeline.event_rebuild.load_provider_config",
                        lambda *_: _async_value(SimpleNamespace(
                            provider="gemini", base_url="https://book.example/v1", api_key=None)))
    monkeypatch.setattr("pipeline.event_rebuild.load_provider_credential",
                        lambda *_: _async_value(("https://account.example/v1", "account-key")))
    connection = await provider_connection(None, _Cfg(), "novel", "gemini")
    assert connection == {"base_url": "https://book.example/v1", "api_key": "account-key"}


@pytest.mark.asyncio
async def test_event_connection_ignores_the_base_url_of_a_book_on_another_provider(monkeypatch):
    """The pinned provider is Gemini; the book has since moved to Ollama.

    Its base_url now names a completely different backend, so borrowing it would point a
    Gemini call at a local Ollama host. Only the account row for the PINNED provider is
    eligible. The key needs no such guard any more -- it is always the account's.
    """
    from types import SimpleNamespace
    monkeypatch.setattr("pipeline.event_rebuild.load_provider_config",
                        lambda *_: _async_value(SimpleNamespace(
                            provider="ollama", base_url="http://localhost:11434", api_key=None)))
    monkeypatch.setattr("pipeline.event_rebuild.load_provider_credential",
                        lambda *_: _async_value(("https://account.example/v1", "account-key")))
    connection = await provider_connection(None, _Cfg(), "novel", "gemini")
    assert connection == {"base_url": "https://account.example/v1", "api_key": "account-key"}


async def _async_value(value):
    return value


def test_unsupported_extraction_provider_fails_closed():
    with pytest.raises(ValueError, match="permits only"):
        EventEngine(None, _Cfg(), _revision("openai"))


@pytest.mark.asyncio
async def test_hosted_extraction_model_pins_without_a_digest():
    # A hosted model has no local digest to pin, so the identity records provider and
    # name only -- and must never imply a reproducibility guarantee it cannot keep.
    pinned = await extraction_model(_Cfg(), "gemini", "gemini-flash")
    assert pinned == {"provider": "gemini", "name": "gemini-flash", "identity": {}}
    assert "digest" not in pinned


@pytest.mark.asyncio
async def test_extraction_model_rejects_provider_outside_the_allowed_set():
    with pytest.raises(ValueError, match="unsupported event extraction provider"):
        await extraction_model(_Cfg(), "openai", "gpt-4")


def test_a_narrative_clause_is_not_a_verb_phrase():
    """The real string translategemma:4b produced, which max_length=80 admitted.

    It is 79 characters, so the only constraint on `action` passed it, and
    canonical_summary then rendered the whole clause as the action. Rejecting the SHAPE
    catches this without reference to which model wrote it (§0: structural, not prompted).
    """
    clause = "An Ruosu's spirits lifted immediately; she began to give a detailed description:"
    assert len(clause) <= 80, "the point is that the length limit does not catch it"
    accepted, rejected = validate_events(
        SOURCE, EventProposals(events=[proposal(action=clause)]), DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert "sentence punctuation" in rejected[0]["rejection"]


def test_a_long_action_without_punctuation_is_still_rejected_as_prose():
    accepted, rejected = validate_events(
        SOURCE,
        EventProposals(events=[proposal(action="walked slowly across the room and then sat down")]),
        DEFAULT_EVENT_SCHEMA, {},
    )
    assert accepted == []
    assert "verb-phrase limit" in rejected[0]["rejection"]


def test_ordinary_verb_phrases_survive_the_shape_guard():
    """The guard must not cost real events. The last of these is a genuine action the
    model produced for chapter 1 and is deliberately inside the word cap."""
    for action in ("handed over", "uprooted", "attacked",
                   "sketched out a simple map on the ground"):
        accepted, rejected = validate_events(
            SOURCE, EventProposals(events=[proposal(action=action)]), DEFAULT_EVENT_SCHEMA, {},
        )
        assert accepted, f"{action!r} should survive: {rejected}"


def _row(event_id, event_type, action, arguments, *, start=0, status="completed"):
    row = {"id": event_id, **proposal(action=action, event_type=event_type, status=status)}
    row.pop("participants")
    row["arguments"] = [{"role": r, "surface": s, "mention_id": None} for r, s in arguments]
    row["evidence_start"] = start
    return row


def test_one_act_extracted_under_three_frames_collapses_to_one():
    """The exact rows v12 published: one pill handoff as transfer, use and communication.

    None of the three share an event_type or a (role, surface) set, so the old key could
    never collide them. They share an action and their core participants, which is what
    makes them one act.
    """
    transfer = _row("event:0", "transfer", "handed over", [
        ("location", "mouth"), ("item", "pill"), ("giver", "Ling Feng"), ("recipient", "Chekov")])
    use = _row("event:1", "use", "handed over", [
        ("actor", "Ling Feng"), ("target", "Chekov"), ("instrument", "hand"), ("item", "pill")])
    communication = _row("event:2", "communication", "handed over", [
        ("actor", "Ling Feng"), ("recipient", "Chekov"), ("subject", "pill")])
    assert len(deduplicate_events([transfer, use, communication])) == 1


def test_distinct_acts_in_one_window_are_not_merged():
    """Chapter 7's real rows: same actor, same evidence window, overlapping participants,
    but different actions — two events, and collapsing them would lose a true fact."""
    plucked = _row("event:0", "acquisition", "plucked", [
        ("actor", "Ares"), ("item", "Star Lotus")])
    handed = _row("event:1", "transfer", "handed over", [
        ("giver", "Ares"), ("item", "Star Lotus"), ("recipient", "Abaddon")])
    assert len(deduplicate_events([plucked, handed])) == 2


def test_a_shared_action_alone_does_not_merge_unrelated_participants():
    """Two handoffs between different people in one window stay separate: the action
    matches but they share fewer than two surfaces."""
    first = _row("event:0", "transfer", "handed over", [
        ("giver", "Ares"), ("item", "lotus"), ("recipient", "Abaddon")])
    second = _row("event:1", "transfer", "handed over", [
        ("giver", "Chekov"), ("item", "pill"), ("recipient", "Lawrence")])
    assert len(deduplicate_events([first, second])) == 2


def test_prompt_version_is_pinned_to_the_code_that_produces_the_prompt():
    """Fail when prompt-affecting code changes without a version bump.

    `chapter-events-v11-schema-required-roles` exists in NO commit: v11 lived only in a
    working tree and was overwritten by v12, so the string was later reattached to a
    reconstruction that behaves differently — on byte-identical chapter-1 input it issues
    one model call where revision d0e3d51e recorded three. Both revisions carry the same
    version, so `activation_eligible` would treat the older one as current under code that
    cannot reproduce it.

    A tripwire, not a proof: it hashes the source of `_call`, the default schema and the
    proposal shape, so a comment edit trips it too. That is the intended bias — being made
    to look is cheap, and silently reusing a version is what caused the problem.
    """
    import hashlib
    import inspect
    from pipeline.events import EventEngine, EventProposal

    material = (
        inspect.getsource(EventEngine._call)
        + json.dumps(DEFAULT_EVENT_SCHEMA, sort_keys=True, ensure_ascii=False)
        + json.dumps(EventProposal.model_json_schema(), sort_keys=True)
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    expected = {"chapter-events-v11-schema-required-roles": "bdbbd8c396c0f6fa"}
    assert EVENT_PROMPT_VERSION in expected, (
        f"EVENT_PROMPT_VERSION is {EVENT_PROMPT_VERSION!r}; add it here with its digest "
        f"({digest!r}) so the new version is recorded against the code that produces it"
    )
    assert digest == expected[EVENT_PROMPT_VERSION], (
        f"prompt-affecting code changed under version {EVENT_PROMPT_VERSION!r} "
        f"(digest {expected[EVENT_PROMPT_VERSION]!r} -> {digest!r}). Bump "
        "EVENT_PROMPT_VERSION and record the new digest: a revision extracted with the old "
        "code must not compare equal to one extracted with this."
    )
