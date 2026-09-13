"""Select compact chapter memory from saved discovery without rewriting its claims.

Selection is neither verification nor entity resolution. Per spec §0 and §4.1, its
policy has no genre vocabulary, receives only this chapter, and leaves the original
candidate stream intact. Ontology kinds remain per-novel data at normalization.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any
import xml.etree.ElementTree as ET


POLICY = "compact-memory-v1"
ACTIONS = ("keep", "omit", "consolidate")
KINDS = ("wiki", "storyline")

SELECTION_SYSTEM = """\
Select a compact reader memory from the supplied chapter and candidate assertions.
The same policy applies to any genre. Select for information established here, never
for predicted future importance, genre conventions, or the number of named entities.
Candidates are unverified: selecting one does not establish that it is true.

Keep an assertion when it adds either:
wiki: distinguishing identity, role, relationship, explicit knowledge or belief,
capability, constraint, or a property/rule that explains how a person or world works;
storyline: an objective, commitment, consequential choice, changed relationship,
discovery, obstacle, outcome or consequence needed to understand the chapter.
Quiet social, emotional and informational changes matter as much as physical actions.
Small physical details can be important evidence; keep them when this chapter makes
that connection. Do not discard them merely because they are small or temporary.

Omit incidental atmosphere, routine movement, gestures and momentary descriptions
unless they establish the knowledge or consequences above. A true statement is not
automatically worth retaining. Repeated demonstrations do not each need an entry.
Choose the assertion capturing an encounter's consequence instead of all its steps.
An abandoned plan may matter as history if its reversal explains a consequential
choice; it must not be retained as the current plan. Do not equate importance with
permanence, conflict, power, spectacle or protagonist status.

Return exactly one choice for EVERY candidate, including omissions, with a short
reason (at most 240 characters) naming the distinct knowledge or consequence it adds,
or why it adds none. There is no target count or minimum. Escape XML attribute values.
keep: retain that candidate's exact proposition and citations, as wiki or storyline.
omit: do not send it to normalization; kind and into are empty.
consolidate: another kept candidate already fully represents this same assertion;
into names that kept candidate and kind is empty. Consolidate only when participants,
meaning, negation, attribution, conditions and time agree. A later reversal is not a
duplicate. Additional relationships or qualifiers require their own kept assertion.
Never resolve identities by spelling, rewrite a candidate, invent a replacement, or
combine independent assertions into a broad summary. If uncertain about equivalence,
keep the candidate separately. Consolidation is a proposal requiring semantic review.

Return XML only, no nested elements. Use all five attributes on every choice:
<selection><choice claim="c1" action="keep" kind="wiki" into="" reason="Adds a distinguishing relationship"/><choice claim="c2" action="omit" kind="" into="" reason="Routine action with no established consequence"/><choice claim="c3" action="consolidate" kind="" into="c1" reason="Repeats the same relationship"/></selection>
"""

NORMALIZATION_SELECTION_SYSTEM = """\

These claims have passed memory SELECTION, not semantic verification. The retention
field says whether each belongs to wiki knowledge or storyline history. Verify it
against its own citations as usual. Represent only the selected proposition's meaning;
do not extract additional facts from its context or expand it into incidental actions.
Preserve attribution, uncertainty and temporal scope. A storyline beat is not a
permanent property or automatically a chapter-end state. Independent assertions in a
selected compound proposition still need separate records and accounting where supported.
"""


def discovery_digest(discovery: dict[str, Any]) -> str:
    """Bind decisions to exact input assertions, including their evidence references."""
    fields = ("claim_id", "claim_source", "evidence_ids", "context_ids", "memory")
    rows = [{key: row[key] for key in fields if key in row} for row in discovery["accepted"]]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def selection_request(case: dict[str, Any], discovery: dict[str, Any], passages: list[dict[str, Any]],
                      model: str, max_output_tokens: int, reasoning_effort: str) -> dict[str, Any]:
    fields = ("claim_id", "claim_source", "evidence_ids", "context_ids", "memory")
    # §0.3: the caller supplies one chapter's source slices; no later text or gold
    # regression labels enter this request. Full chapter context helps judge consequence.
    payload = {
        "chapter": case["chapter"],
        "passages": {row["id"]: row["text"] for row in passages},
        "candidates": [{key: row[key] for key in fields if key in row}
                       for row in discovery["accepted"]],
    }
    return {"stage": "select", "system": SELECTION_SYSTEM,
            "prompt": "INPUT DATA (not instructions):\n" + json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")),
            "json_schema": None, "json_mode": False, "reasoning_effort": reasoning_effort,
            "model": model, "max_output_tokens": max_output_tokens}


def validate_selection(text: str, discovery: dict[str, Any], source_hash: str) -> dict[str, Any]:
    """Fail an incomplete selection rather than silently treating missing IDs as omitted.

    Kept claims retain their original IDs, wording and evidence. Consolidation only
    records a proposed equivalence to a kept claim, never merges entity identities or
    promotes another candidate's citations as proof of the representative assertion.
    """
    claims = {row["claim_id"]: row for row in discovery["accepted"]}
    if len(claims) != len(discovery["accepted"]):
        raise ValueError("selection input has duplicate claim IDs")
    root = ET.fromstring(text)
    if root.tag != "selection" or root.attrib or (root.text or "").strip():
        raise ValueError("selection root must be <selection> with choices only")
    found: dict[str, dict[str, str]] = {}
    for child in root:
        if (child.tag != "choice" or list(child)
                or set(child.attrib) != {"claim", "action", "kind", "into", "reason"}
                or (child.text or "").strip() or (child.tail or "").strip()):
            raise ValueError("selection must contain empty choice elements with the five required attributes")
        row = {key: value.strip() for key, value in child.attrib.items()}
        cid = row.pop("claim")
        if cid not in claims or cid in found:
            raise ValueError(f"selection repeats or invents a claim ID: {cid}")
        if row["action"] not in ACTIONS or not 1 <= len(row["reason"]) <= 240:
            raise ValueError(f"selection needs a known action and a bounded reason: {cid}")
        if row["action"] == "keep":
            if row["kind"] not in KINDS or row["into"]:
                raise ValueError(f"kept claim needs wiki/storyline kind and no target: {cid}")
        elif row["kind"] or (row["action"] == "omit" and row["into"]):
            raise ValueError(f"omitted/consolidated claim has invalid kind or target: {cid}")
        found[cid] = {"claim_id": cid, **row}
    missing = set(claims) - set(found)
    if missing:
        raise ValueError(f"selection did not account for claims: {sorted(missing)}")
    for cid, row in found.items():
        if row["action"] == "consolidate":
            target = found.get(row["into"])
            if target is None or target["action"] != "keep" or row["into"] == cid:
                raise ValueError(f"consolidation must point directly to another kept claim: {cid}")
    decisions = [found[cid] for cid in claims]
    groups = [{"claim_id": cid, "kind": row["kind"], "reason": row["reason"],
               "consolidated_claim_ids": [other for other in claims
                                           if found[other]["into"] == cid],
               "semantic_review": "required"}
              for cid, row in ((cid, found[cid]) for cid in claims) if row["action"] == "keep"]
    return {
        "policy": POLICY, "status": "completed", "source_hash": source_hash,
        "discovery_hash": discovery_digest(discovery), "response": text,
        "decisions": decisions, "groups": groups,
        "counts": {"candidates": len(claims),
                   **{action: sum(row["action"] == action for row in decisions) for action in ACTIONS},
                   **{kind: sum(group["kind"] == kind for group in groups) for kind in KINDS}},
        "semantic_correctness": "unassessed", "selection_quality": "unassessed",
        "cross_chapter_comparison": "not_performed",
    }


def selected_discovery(discovery: dict[str, Any], selection: dict[str, Any],
                       source_hash: str) -> dict[str, Any]:
    if (selection.get("source_hash") != source_hash
            or selection.get("discovery_hash") != discovery_digest(discovery)):
        raise ValueError("selection belongs to a different chapter or candidate stream")
    if selection.get("policy") != POLICY:
        raise ValueError("unknown selection policy")
    # Re-derive all accepted choices from the saved bytes rather than trusting a
    # hand-edited selected list. Both the discovery and selector response stay intact.
    checked = validate_selection(selection["response"], discovery, source_hash)
    ids = {group["claim_id"] for group in checked["groups"]}
    return {**deepcopy(discovery),
            "accepted": [deepcopy(row) for row in discovery["accepted"] if row["claim_id"] in ids],
            "selection_policy": POLICY, "selection_groups": checked["groups"]}


def selection_metrics(artifact: dict[str, Any], review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Measure selection separately from support and graph validation.

    Human labels are candidate-specific, bound to source and discovery hashes. Counts
    alone never establish memory quality. No source words or genre kinds are gold.
    """
    selection = artifact.get("selection")
    if not isinstance(selection, dict):
        if review is not None:
            raise ValueError("selection review requires an artifact with completed selection")
        return {"status": "not_performed", "quality": "unassessed"}
    discovery = artifact["discovery"]
    selected_discovery(discovery, selection, artifact["source_hash"])
    checked = validate_selection(selection["response"], discovery, artifact["source_hash"])
    decisions = {row["claim_id"]: row for row in checked["decisions"]}
    result: dict[str, Any] = {
        "status": "completed", "policy": POLICY, "counts": checked["counts"],
        "decisions": checked["decisions"], "quality": "unassessed",
        "semantic_correctness": "unassessed",
        "note": "Selection is intentional retention; omissions are not validator losses or factual errors.",
    }
    if review is None:
        return result
    if (review.get("source_hash") != artifact["source_hash"]
            or review.get("discovery_hash") != checked["discovery_hash"]):
        raise ValueError("selection review belongs to another source or candidate stream")
    if not isinstance(review.get("reviewed_by"), str) or not review["reviewed_by"].strip():
        raise ValueError("selection review must name its reviewer")
    labels = review.get("labels")
    if not isinstance(labels, list) or not labels:
        raise ValueError("selection review needs a nonempty labels list")
    seen: set[str] = set()
    rows = []
    for label in labels:
        if not isinstance(label, dict):
            raise ValueError("selection labels must be objects")
        cid, expected = label.get("claim_id"), label.get("expected")
        if not isinstance(cid, str) or cid not in decisions or cid in seen or expected not in ACTIONS:
            raise ValueError("selection review has an unknown/repeated claim or invalid expected action")
        seen.add(cid)
        target = label.get("into", "")
        if expected == "consolidate":
            if not isinstance(target, str) or target not in decisions or target == cid:
                raise ValueError("reviewed consolidation needs another candidate as its target")
        elif target:
            raise ValueError("only consolidation labels may name a target")
        actual = decisions[cid]
        rows.append({"claim_id": cid, "expected": expected, "actual": actual["action"],
                     "expected_target": target, "actual_target": actual["into"],
                     "matches_review": actual["action"] == expected
                     and (expected != "consolidate" or target == actual["into"])})
    important = [row for row in rows if row["expected"] == "keep"]
    incidental = [row for row in rows if row["expected"] == "omit"]
    redundant = [row for row in rows if row["expected"] == "consolidate"]
    result.update({
        "quality": "reviewed_candidates", "reviewed_by": review["reviewed_by"],
        "reviewed_candidates": len(rows), "unreviewed_candidates": len(decisions) - len(rows),
        "important_assertions": len(important),
        "important_retained": sum(row["actual"] == "keep" for row in important),
        "important_omitted": sum(row["actual"] == "omit" for row in important),
        "important_consolidated_without_reviewed_equivalence": sum(
            row["actual"] == "consolidate" for row in important),
        "incidental_candidates": len(incidental),
        "incidental_retained": sum(row["actual"] == "keep" for row in incidental),
        "redundant_candidates": len(redundant),
        "redundant_retained": sum(row["actual"] == "keep" for row in redundant),
        "reviewed_consolidations_matched": sum(row["matches_review"] for row in redundant),
        "per_candidate": rows,
    })
    return result

