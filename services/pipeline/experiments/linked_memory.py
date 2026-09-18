"""Validate joint notes/local identity without equating matching names (§0, §5).

No production publication: semantic identity and factual support require source review.
"""
import json

from pipeline.fact_first import _source_passages


def validate_linked_memory(text, case):
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"entities", "notes"}:
        raise ValueError("expected entities and notes")
    if not isinstance(data["entities"], list) or not isinstance(data["notes"], list):
        raise ValueError("expected entity and note arrays")
    passages = {p["id"]: p["text"] for p in _source_passages(case["source"])}
    declared, accepted, issues = set(), {}, []

    def citations(value):
        return (isinstance(value, list) and bool(value)
                and all(isinstance(p, str) and p in passages for p in value))

    for entity in data["entities"]:
        if not isinstance(entity, dict) or set(entity) != {"id", "names", "passages"}:
            raise ValueError("invalid entity shape")
        eid, names, pids = entity["id"], entity["names"], entity["passages"]
        if not isinstance(eid, str) or not eid or eid in declared:
            raise ValueError("invalid or duplicate entity ID")
        declared.add(eid)
        if (not isinstance(names, list) or not names
                or any(not isinstance(n, str) or not n for n in names)):
            raise ValueError("invalid source names")
        if not citations(pids) or any(not any(n in passages[p] for p in pids) for n in names):
            issues.append({"entity": eid, "issue": "unwitnessed name or invalid citation"})
            continue
        accepted[eid] = entity
    checked = []
    for index, note in enumerate(data["notes"], 1):
        if not isinstance(note, dict) or set(note) != {"note", "passages", "entities", "unresolved"}:
            raise ValueError("invalid note shape")
        if not isinstance(note["note"], str) or not note["note"].strip():
            raise ValueError("missing note text")
        ids, unresolved = note["entities"], note["unresolved"]
        if not isinstance(ids, list) or any(not isinstance(e, str) or e not in declared for e in ids):
            raise ValueError("note references undeclared entity")
        if not isinstance(unresolved, list) or any(not isinstance(r, str) or not r for r in unresolved):
            raise ValueError("invalid unresolved references")
        problems = []
        if not citations(note["passages"]):
            problems.append("invalid note citations")
        if any(e not in accepted for e in ids):
            problems.append("links to ungrounded entity")
        if any(r in passages or not any(r in text for text in passages.values()) for r in unresolved):
            problems.append("unwitnessed unresolved reference")
        if problems:
            issues.append({"note": index, "issues": problems})
        checked.append({**note, "entities": list(dict.fromkeys(e for e in ids if e in accepted)),
                        "issues": problems})
    return {"entities": list(accepted.values()), "notes": checked, "issues": issues,
            "structurally_valid": not issues, "semantic_review": "pending",
            "published": False}
