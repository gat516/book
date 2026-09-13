"""Memory candidate contract for the standalone benchmark; no persistence or resolution.

Source references are deliberately unresolved. A future append planner must resolve
identities and compare meanings using knowledge available at the reader's chapter (§0).
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class MemoryMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["wiki", "storyline"]
    subject: Short
    predicate: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
    object: Short
    basis: Literal["stated", "demonstrated", "reported"]
    attribution: OptionalText
    temporal: Literal["ongoing", "prior", "planned", "conditional", "unknown"]
    qualifiers: OptionalText


DISCOVERY_SYSTEM = """\
Extract a compact memory of this Chinese chapter, not a transcript of its actions.
wiki: identities, relationships, abilities, constraints, and identifying descriptions
of people, places or objects. Preserve destinations, access routes, resource properties
and grades, and attributed estimates. An ability demonstrated several times needs one
ability assertion, not a fact for every attack. Do not invent permanent abilities from
figurative language, equipment effects, or someone else's actions.
storyline: the party's objective/agreement, plan, changed obstacle, outcome or consequence.
Summarize an encounter's effect on that objective; omit individual dodges, gestures,
shouted attacks and routine treatment unless they establish a distinct wiki assertion
or change the storyline. Unnamed opponents need not become individual entities.

Usually a handful of wiki assertions and 1-3 storyline beats suffice; counts are not
quotas. One claim must be independently comparable: split impersonation, theft and
killing; separate a rider's identity from a mount's ability. Do not bury another
assertion in qualifiers or group crimes under a vague relationship. A charge or
formation belongs to storyline, not a permanent property. Deduplicate repeated uses.

Ingestion may begin halfway through the story. First observed does not mean newly
acquired or first met. Preserve references to prior events without dating them to this
chapter. Check who commands versus who obeys, who benefits ("us" is not "you"),
and whether an offer is conditional ("may consider" is not a promise). Preserve these
distinctions in the proposition. A reaction followed by detection alone does not prove
a device's sensing mechanism. Cite support for every qualifier; use no outside knowledge.

Return XML only, at most 30 claims. evidence/context list passage IDs separated by commas.
subject/object use source descriptions, not invented IDs. predicate is ASCII snake_case.
kind=wiki|storyline; basis=stated|demonstrated|reported;
temporal=ongoing|prior|planned|conditional|unknown. attribution is the source speaker
when known, otherwise empty. qualifiers preserve restrictions/estimates, otherwise empty.
Use this exact shape; escape XML values:
<claims><claim id="c1" evidence="p001" context="" kind="wiki" subject="人物原名" predicate="can_use" object="能力原名" basis="demonstrated" attribution="" temporal="unknown" qualifiers="">完整中文命题</claim></claims>
"""


def candidates(case: dict[str, Any], discovery: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    """Prepare comparison inputs, never call them new facts or append instructions."""
    decisions = {row["claim_id"]: row for row in extraction["accepted"]["decisions"]}
    diagnostics = {row["claim_id"]: row for row in extraction.get("claim_diagnostics", [])}
    source_hash = hashlib.sha256(case["source"].encode()).hexdigest()
    result: dict[str, Any] = {"wiki": [], "storyline": [], "rejected": [],
                              "comparison_status": "not_compared", "persistence": "not_implemented"}
    seen: dict[str, str] = {}
    for claim in discovery["accepted"]:
        cid = claim["claim_id"]
        if decisions.get(cid, {}).get("verdict") != "supported":
            continue
        metadata = MemoryMetadata.model_validate(claim["memory"]).model_dump()
        # This is only a matching hint. Neither literal equality nor a matching hash
        # resolves entity identity or establishes semantic equivalence across chapters.
        signature = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        diagnostic = diagnostics.get(cid, {})
        records = diagnostic.get("accepted_records", [])
        row = {
            "claim_id": cid, "statement": claim["claim_source"], **metadata,
            "subject_reference": {"source": metadata["subject"], "entity_id": None,
                                  "resolution_status": "unresolved"},
            "source_chapter": case["chapter"], "source_hash": source_hash,
            "valid_from_chapter": None, "acquired_at_chapter": None,
            "prior_history_status": "not_assessed", "comparison_status": "not_compared",
            "comparison_hint": signature, "exact_metadata_duplicate_of": seen.get(signature),
            "evidence": claim.get("evidence", []), "context": claim.get("context", []),
            "support_decision": decisions[cid], "semantic_review": "required",
            "accepted_graph_records": records,
            "representation_status": "has_records" if records else "unrepresented",
            "validation_rejections": diagnostic.get("validation_rejections", []),
            "attribution_unresolved": metadata["basis"] == "reported" and not metadata["attribution"],
        }
        seen.setdefault(signature, cid)
        result[metadata["kind"]].append(row)
    return result

