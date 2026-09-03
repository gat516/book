from __future__ import annotations

import copy

from pipeline.events import (
    DEFAULT_EVENT_SCHEMA,
    EventProposals,
    canonical_summary,
    deduplicate_events,
    validate_events,
)
from pipeline.passages import PassageContract
from pipeline.passages import source_windows
from pipeline.event_rebuild import qualified, retry_delay_minutes


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
