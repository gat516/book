"""Production records pipeline primitives.

The parser and deterministic checks are deliberately independent of persistence and
provider code.  Worker and API layers can therefore freeze their inputs and publish
the resulting immutable rows transactionally.  Who's-who is the only identity
authority: this module validates its assignments but never matches names itself.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import uuid
from typing import Any

RECORD_TYPES: dict[str, tuple[str, ...]] = {
    "EVENT": ("what", "who", "outcome", "told"),
    "SPEECH": ("speaker", "act", "addressee", "content", "accepted"),
    "STATE": ("character", "goal", "knows", "unknown", "condition", "location"),
    "RELATION": ("side_a", "side_b", "kind", "polarity"),
    "PROMISE": ("promiser", "promisee", "promised", "status"),
    "ABILITY": ("character", "ability", "effect"),
    "WORLD": ("fact", "scope"),
    "IDENTITY": ("entity", "is", "of"),
}
NAME_FIELDS = {
    "EVENT": ("who",), "SPEECH": ("speaker", "addressee"),
    "STATE": ("character",), "RELATION": ("side_a", "side_b"),
    "PROMISE": ("promiser", "promisee"), "ABILITY": ("character",),
    "IDENTITY": ("entity", "of"),
}
ACTOR_FIELDS = {
    "EVENT": ("who",), "SPEECH": ("speaker",), "STATE": ("character",),
    "PROMISE": ("promiser",), "ABILITY": ("character",), "IDENTITY": ("entity",),
}
CONTENT_FIELDS = {"STATE": ("goal", "knows", "unknown")}
NAME_SEPARATORS = re.compile(r"[、，,;；/]")
CJK = re.compile(r"[一-鿿]")
CHECK_WINDOW = 1
CONTENT_SUPPORT = 0.6


def _body(text: str) -> str:
    match = re.search(r"<records\b.*</records>", text or "", re.S)
    return match.group(0) if match else (text or "")


def split_names(value: str) -> list[str]:
    return [part.strip() for part in NAME_SEPARATORS.split(value or "") if part.strip()]


def name_written(name: str, text: str) -> bool:
    if name in text:
        return True
    parts = [p for p in re.split(r"[·•・]", name) if len(p) >= 2]
    return len(parts) > 1 and any(p in text for p in parts)


def parse_records(text: str, passages: dict[str, str] | set[str]) -> dict[str, Any]:
    """Parse XML one record at a time, preserving malformed/drop diagnostics."""
    passage_ids = set(passages)
    problems: list[dict[str, Any]] = []
    elements: list[tuple[int, ET.Element]] = []
    body = _body(text)
    try:
        root = ET.fromstring(body)
        if root.tag != "records":
            # Small local models commonly omit the container when they emit exactly
            # one compact typed record.  Treat the known, unambiguous typed element
            # as that one record; unknown roots remain document errors.
            if root.tag.upper() in RECORD_TYPES:
                elements = [(0, root)]
                problems.append({"where": "document", "problem": "missing <records> wrapper; recovered typed record"})
            else:
                problems.append({"where": "document", "problem": f"root is <{root.tag}>, expected <records>"})
                elements = list(enumerate(root))
        else:
            elements = list(enumerate(root))
        document = "well_formed"
    except ET.ParseError as exc:
        document = "malformed"
        problems.append({"where": "document", "problem": f"malformed XML ({exc})"})
        # A sequence of individually well-formed <STATE .../>, <EVENT .../>, etc.
        # has multiple XML roots, but each assertion is still deterministic to read.
        # Wrapping only parses structure; every recovered record still goes through
        # the same type, field, evidence, and grounding checks below.
        try:
            recovered = ET.fromstring(f"<records>{body}</records>")
            elements = list(enumerate(recovered))
            document = "recovered_fragments"
            problems.append({"where": "document", "problem": "missing <records> wrapper; recovered record list"})
        except ET.ParseError:
            pass
        for position, block in enumerate(re.findall(r"<record\b.*?</record>", body, re.S)) if not elements else []:
            try:
                elements.append((position, ET.fromstring(block)))
            except ET.ParseError as block_exc:
                problems.append({"where": f"record at position {position}", "problem": f"unreadable: {block_exc}", "raw": block[:400]})
    records: list[dict[str, Any]] = []
    for position, element in elements:
        typed_element = element.tag.upper() in RECORD_TYPES
        if element.tag != "record" and not typed_element:
            problems.append({"where": f"position {position}", "problem": f"<{element.tag}> is not a <record>"})
            continue
        rtype = element.tag.upper() if typed_element else (element.get("type") or "").strip().upper()
        evidence = [p.strip() for p in re.split(r"[,;、，\s]+", element.get("evidence") or "") if p.strip()]
        issues: list[str] = []
        fields: dict[str, str] = {}
        # Compact typed elements use named attributes, for example
        # <STATE evidence="p..." character="..." location="..."/>.  Attribute
        # names are just as explicit as named child tags and require no guessing.
        if typed_element:
            fields.update({name: value.strip() for name, value in element.attrib.items()
                           if name not in {"evidence", "type"} and value.strip()})
        for child in element:
            # Named tags are the canonical wire shape (<character>凌峰</character>).
            # Accept <field name="character"> as an equally unambiguous shape, but
            # never guess what repeated bare <field> nodes mean: doing so can pair a
            # model-generated label with the wrong value and publish a false record.
            field = (child.get("name") or "").strip() if child.tag == "field" else child.tag
            if not field:
                issues.append("generic <field> is missing a name attribute")
                continue
            if field in fields:
                issues.append(f"duplicate field {field!r}")
                continue
            fields[field] = (child.text or "").strip()
        if rtype not in RECORD_TYPES:
            issues.append(f"unknown record type {rtype or '(none)'}")
        else:
            extra = [k for k in fields if k not in RECORD_TYPES[rtype] and k != "quote"]
            if extra:
                issues.append(f"fields not defined for {rtype}: {extra}")
            if not fields and not issues:
                issues.append("contains no non-empty fields")
        unknown = [p for p in evidence if p not in passage_ids]
        if unknown: issues.append(f"cites passages that do not exist: {unknown}")
        if not evidence: issues.append("cites no passage")
        records.append({"index": len(records), "position": position, "type": rtype,
                        "evidence_ids": evidence, "fields": fields, "issues": issues,
                        "usable": rtype in RECORD_TYPES and bool(evidence) and not issues})
    return {"document": document, "records": records, "problems": problems,
            "counts": {"records": len(records), "usable": sum(r["usable"] for r in records)}}


def check_records(records: list[dict[str, Any]], passages: dict[str, str]) -> dict[str, Any]:
    dropped: list[dict[str, Any]] = []
    ordered_passages = list(passages)
    passage_positions = {passage_id: index for index, passage_id in enumerate(ordered_passages)}
    for record in records:
        if not record["usable"]: continue
        cited = [p for p in record["evidence_ids"] if p in passages]
        # Passage IDs are content-scoped (for example p147_a3596019ae7e), not ordinal
        # p001 labels. Build the local context window from the offered passage order so
        # the cited passage is always checked and adjacent context remains available.
        window_indexes = {
            position + delta
            for passage_id in cited
            for position in [passage_positions[passage_id]]
            for delta in range(-CHECK_WINDOW, CHECK_WINDOW + 1)
            if 0 <= position + delta < len(ordered_passages)
        }
        window_text = "".join(passages[ordered_passages[index]] for index in sorted(window_indexes))
        failures: list[str] = []
        for field in ACTOR_FIELDS.get(record["type"], ()):
            for name in split_names(record["fields"].get(field, "")):
                if not name_written(name, window_text): failures.append(f"{field} {name!r} is not grounded near citation")
        quote = record["fields"].get("quote", "")
        if quote and not any(quote in passages[p] for p in cited): failures.append("quote is not verbatim in cited passage")
        for field in CONTENT_FIELDS.get(record["type"], ()):
            value = record["fields"].get(field, "")
            chars = CJK.findall(value)
            support = sum(c in window_text for c in chars) / len(chars) if chars else 0
            if value and support < CONTENT_SUPPORT: failures.append(f"{field} is weakly grounded ({support:.0%})")
        if failures:
            record["usable"] = False; record["check_failures"] = failures
            dropped.append({"record": record["index"], "type": record["type"], "failures": failures})
    return {"dropped": dropped, "kept": sum(r["usable"] for r in records)}


def collect_names(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record["usable"]: continue
        for field in NAME_FIELDS.get(record["type"], ()):
            for name in split_names(record["fields"].get(field, "")):
                row = result.setdefault(name, {"id": f"n{len(result)+1}", "name": name, "mentions": [], "passages": []})
                row["mentions"].append(f"{record['type']}.{field}")
                row["passages"] += [p for p in record["evidence_ids"] if p not in row["passages"]]
    return list(result.values())


def unresolved(names: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    refs = [{"id": f"r{i}", "surface": n["name"], "refers_to": "unknown", "candidate_entity_id": None, "reason": reason} for i, n in enumerate(names, 1)]
    return {"entities": [], "references": refs, "name_map": {r["surface"]: r["id"] for r in refs}, "problems": [reason]}


def validate_resolution(text: str, names: list[dict[str, Any]], kinds: list[str],
                        candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Accept only complete, unique, ontology-valid who's-who assignments."""
    by_id = {n["id"]: n for n in names}; assigned: dict[str, str] = {}; entities=[]; refs=[]; problems=[]
    supplied = {str(c["id"]): c for c in (candidates or [])}
    root = ET.fromstring(_body(text))
    if root.tag != "resolution": raise ValueError("resolution root expected")
    # Preflight the complete response. A duplicate member can otherwise make the
    # first group look valid while the later group is rejected, silently assigning a
    # referent according to XML order. Invalidation is group-wide and deterministic.
    entity_nodes = root.findall("entity")
    member_groups: dict[str, list[int]] = {}
    entity_ids: dict[str, list[int]] = {}
    for i, node in enumerate(entity_nodes):
        entity_ids.setdefault((node.get("id") or "").strip(), []).append(i)
        for member in re.split(r"[,;、，\s]+", node.get("names") or ""):
            member = member.strip()
            if member: member_groups.setdefault(member, []).append(i)
    invalid_groups = {i for groups in entity_ids.values() if len(groups) > 1 for i in groups}
    invalid_groups |= {i for groups in member_groups.values() if len(groups) > 1 for i in groups}
    seen: set[str] = set()
    for node_index, node in enumerate(entity_nodes):
        eid=(node.get("id") or "").strip(); kind=(node.get("kind") or "").strip(); members=[x.strip() for x in re.split(r"[,;、，\s]+", node.get("names") or "") if x.strip()]
        reason = "duplicate entity id or name assignment" if node_index in invalid_groups else None
        reason = reason or (None if re.fullmatch(r"e[0-9]{1,4}", eid) and eid not in seen else "bad or repeated entity id")
        reason = reason or (None if kind in kinds else f"kind {kind!r} is not in ontology")
        reason = reason or next((f"unknown name id {m}" for m in members if m not in by_id), None)
        reason = reason or next((f"name {m} already assigned" for m in members if m in assigned), None)
        if reason: problems.append(f"entity {eid or '?'} rejected: {reason}"); continue
        canonical=(node.get("canonical") or "").strip(); strings=[by_id[m]["name"] for m in members]
        canonical = canonical if canonical in strings else max(strings, key=len)
        candidate = (node.get("candidate") or "").strip() or None
        if candidate is not None and candidate not in supplied:
            problems.append(f"entity {eid}: candidate {candidate!r} was not supplied")
            continue
        seen.add(eid)
        entities.append({"id":eid,"kind":kind,"canonical":canonical,"names":strings,
                        "candidate_entity_id": candidate})
        assigned.update({m:eid for m in members})
    for node in root.findall("reference"):
        nid=(node.get("name") or "").strip()
        if nid not in by_id or nid in assigned: problems.append(f"reference {nid!r} ignored"); continue
        kind=(node.get("refers_to") or "unknown").strip(); kind=kind if kind in kinds or kind=="unknown" else "unknown"
        candidate=(node.get("candidate") or "").strip() or None
        if candidate and candidate not in supplied:
            problems.append(f"reference {nid}: candidate {candidate!r} was not supplied")
            candidate=None
        rid=f"r{len(refs)+1}"; assigned[nid]=rid; refs.append({"id":rid,"surface":by_id[nid]["name"],"refers_to":kind,"candidate_entity_id":candidate})
    for n in names:
        if n["id"] not in assigned:
            rid=f"r{len(refs)+1}"; assigned[n["id"]]=rid; refs.append({"id":rid,"surface":n["name"],"refers_to":"unknown","candidate_entity_id":None,"reason":"not placed"})
    return {"entities":entities,"references":refs,
            # Structural proposal identity is keyed by the resolver input ID (for
            # fact-first this is the normalized local proposal ID), never by an
            # English rendering or a spelling lookup.
            "proposal_map": dict(assigned),
            "name_map":{by_id[k]["name"]:v for k,v in assigned.items()},"problems":problems}


def deterministic_entity_id(scope_id: str, proposal_id: str) -> str:
    """Stable UUID for a new who's-who proposal.

    ``scope_id`` is the immutable run UUID, rather than only the generation: two
    chapters may both call their first proposal ``e1`` and must remain distinct.
    """
    return str(uuid.uuid5(uuid.UUID(scope_id), f"entity:{proposal_id}"))


def build_rows(records: list[dict[str, Any]], resolution: dict[str, Any]) -> list[dict[str, Any]]:
    rows=[]
    for record in records:
        if not record["usable"]: continue
        participants={}; values={}
        for field,value in record["fields"].items():
            if not value: continue
            if field in NAME_FIELDS.get(record["type"], ()):
                participants[field]=[{"surface": n, **(({"entity_id": resolution["name_map"].get(n)} if resolution["name_map"].get(n, "").startswith("e") else {"reference_id": resolution["name_map"].get(n)}))} for n in split_names(value)]
            # Keep the original pair even when it is also represented structurally as
            # a participant. This is the lossless record_value contract: rendering and
            # future readers must be able to reproduce the source assertion exactly.
            values[field]=value
        rows.append({"record_index":record["index"],"type":record["type"],"evidence_ids":record["evidence_ids"],"participants":participants,"values":values})
    return rows
