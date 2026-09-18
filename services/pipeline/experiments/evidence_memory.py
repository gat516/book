"""Evidence-first experimental contracts. No persistence, provider calls or name matching authority.

Spec §0: source and knowledge chapter are immutable; source evidence is never a
normalized assertion. Code may retrieve candidates, but only model decisions bind IDs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid

from pipeline.fact_first import _source_passages

HERE = Path(__file__).parent
CHECKS_VERSION = "evidence-checks-v4-lines"
ENRICHMENT_VERSION = "evidence-context-v2"


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(wire(value).encode()).hexdigest()


def decode(text):
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.S | re.I)
    return json.loads(match[1] if match else text)


def policy_for(override=None):
    policy = json.loads((HERE / "evidence-policy.json").read_text())
    if override is not None:
        if not isinstance(override, dict) or set(override) - set(policy):
            raise ValueError("unknown evidence policy fields")
        policy.update(override)
    priorities = policy["priorities"]
    if (not isinstance(priorities, dict) or not priorities
            or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
                   for k, v in priorities.items())):
        raise ValueError("priorities must map nonempty IDs to descriptions")
    for key in ("candidate_limit", "identity_witness_target", "max_enrichment_input_tokens"):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ValueError("policy budgets must be positive integers")
    if type(policy["max_context_tokens"]) is not int or policy["max_context_tokens"] < 0:
        raise ValueError("context budget must be a nonnegative integer")
    return policy


def passages_for(source):
    return {i: {**p, "id": i} for i, p in enumerate(_source_passages(source), 1)}


def strings(value, *, nonempty=False):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(s, str) and bool(s.strip()) for s in value))


def passage_ids(value, passages, diagnostics, where):
    """Recover ordinal formatting only, never a nonexistent or substituted citation."""
    if not isinstance(value, list) or not value:
        raise ValueError("missing passage IDs")
    ids = []
    for original in value:
        pid = original
        if isinstance(pid, str) and re.fullmatch(r"p?\d+", pid):
            pid = int(pid.removeprefix("p"))
        if type(pid) is not int or pid not in passages:
            raise ValueError("unknown passage ID")
        if type(original) is not int:
            diagnostics.append({"where": where, "action": "normalize ordinal", "from": original, "to": pid})
        if pid not in ids:
            ids.append(pid)
    return ids


def retrieve(source, earlier, cap):
    """Retrieve aliases and lexical alternatives; never turn them into identity (§5)."""
    bigrams = {source[i:i + 2] for i in range(len(source) - 1)}
    ranked = []
    for entity in earlier:
        exact = sum(len(n) for n in entity["names"] if n in source)
        fuzzy = sum(len({n[i:i + 2] for i in range(len(n) - 1)} & bigrams) for n in entity["names"])
        if exact or fuzzy:
            ranked.append((exact * 10 + fuzzy, entity))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [e for _, e in ranked[:cap]]


def earlier_entities(artifacts, *, novel_id, chapter, generation):
    """Reject mixed books/generations and future artifacts before prompt construction (§0.3)."""
    registry = {}
    for artifact in sorted(artifacts, key=lambda a: a["source_chapter"]):
        if (artifact.get("novel_id") != novel_id or artifact.get("generation") != generation
                or type(artifact.get("source_chapter")) is not int
                or not 0 < artifact["source_chapter"] < chapter
                or artifact.get("format") != "evidence-memory-v1"):
            raise ValueError("prior evidence must belong to this book/generation and an earlier chapter")
        for entity in artifact["result"]["entities"]:
            if not entity.get("identity_id"):
                continue
            eid = entity["identity_id"]
            names = registry[eid]["names"] if eid in registry else []
            registry[eid] = {"id": eid, "kind": entity["kind"],
                             "names": list(dict.fromkeys(names + entity["names"]))}
    return list(registry.values())


def extraction_request(case, policy, candidates, *, model, reasoning, output_tokens):
    passages = passages_for(case["source"])
    # One completion follows explicit reading windows without repeated source
    # text or per-window calls (spec §0.7). These are not scene boundaries.
    ids = list(passages)
    sections = [[ids[i], ids[min(i + 23, len(ids) - 1)]] for i in range(0, len(ids), 24)]
    payload = {"priorities": policy["priorities"], "kinds": case["ontology"]["kinds"],
               "identity_witness_target": policy["identity_witness_target"],
               "reading_sections": sections,
               "candidates": candidates, "passages": [[i, p["text"]] for i, p in passages.items()]}
    return {"system": (HERE / "prompts/evidence-select-v1.txt").read_text(),
            "prompt": wire(payload), "model": model, "max_output_tokens": output_tokens,
            "reasoning_effort": reasoning, "json_mode": False}


def validate_selection(text, case, policy, candidates, namespace):
    data = decode(text)
    if (not isinstance(data, dict) or set(data) != {"entities", "records"}
            or not isinstance(data["entities"], list) or not isinstance(data["records"], list)):
        raise ValueError("expected entities and records arrays")
    passages = passages_for(case["source"])
    candidates = {e["id"]: e for e in candidates}
    diagnostics, accepted, seen = [], {}, set()
    rejected_names, unsupported_aliases, declared_names = {}, {}, {}
    for entity in data["entities"]:
        if not isinstance(entity, dict) or set(entity) != {"id", "kind", "names", "passages", "same_as"}:
            raise ValueError("invalid entity shape")
        eid = entity["id"]
        if not isinstance(eid, str) or not eid or eid in seen:
            raise ValueError("invalid or duplicate local entity ID")
        seen.add(eid)
        if not strings(entity["names"], nonempty=True):
            raise ValueError("invalid entity names")
        declared_names[eid] = entity["names"]
        try:
            pids = passage_ids(entity["passages"], passages, diagnostics, eid)
            kind, decision = entity["kind"], entity["same_as"]
            if kind not in case["ontology"]["kinds"]:
                raise ValueError("kind absent from novel ontology")
            if decision is not None and (not isinstance(decision, str) or
                                        decision != "new" and decision not in candidates):
                raise ValueError("identity decision references unknown candidate")
            if decision in candidates and candidates[decision]["kind"] != kind:
                raise ValueError("candidate kind conflict")
            grounded = [n for n in entity["names"] if any(n in passages[p]["text"] for p in pids)]
            if not grounded or grounded[0] != entity["names"][0]:
                raise ValueError("canonical name has no source witness")
            if grounded != entity["names"]:
                diagnostics.append({"where": eid, "action": "omit unwitnessed aliases"})
                unsupported_aliases[eid] = [n for n in entity["names"] if n not in grounded]
            identity = (str(uuid.uuid5(uuid.NAMESPACE_URL, namespace + ":" + eid))
                        if decision == "new" else decision)
            accepted[eid] = {**entity, "names": list(dict.fromkeys(grounded)), "passages": pids,
                             "identity_id": identity}
        except (ValueError, TypeError):
            rejected_names[eid] = entity["names"]
            diagnostics.append({"where": eid, "action": "leave invalid identity unresolved"})
    records, record_ids = [], set()
    for row in data["records"]:
        if not isinstance(row, dict) or set(row) != {"id", "topic", "passages", "entities", "unresolved"}:
            raise ValueError("invalid evidence record shape")
        rid = row["id"]
        if not isinstance(rid, str) or not rid or rid in record_ids:
            raise ValueError("invalid or duplicate record ID")
        record_ids.add(rid)
        if not strings(row["entities"]) or not strings(row["unresolved"]):
            raise ValueError("invalid participant references")
        try:
            pids = passage_ids(row["passages"], passages, diagnostics, rid)
            if row["topic"] not in policy["priorities"]:
                raise ValueError("unrequested topic")
        except (ValueError, TypeError):
            diagnostics.append({"where": rid, "action": "drop unsupported evidence selection"})
            continue
        local_ids = list(dict.fromkeys(row["entities"]))
        unresolved = []
        for reference in row["unresolved"]:
            if reference in declared_names:
                # Recover the surface, never an identity binding. The model
                # explicitly left this participant unresolved (spec §5).
                names = [n for n in declared_names[reference] if n in case["source"]]
                unresolved.extend(names or [f"unknown local ID: {reference}"])
                diagnostics.append({"where": rid, "action": "expand unresolved local ID to source names",
                                    "reference": reference})
            else:
                unresolved.append(reference)
        for eid in local_ids:
            # Keep unsupported aliases visible for review; a supported primary
            # identity does not authorize its unwitnessed alternate names (§5).
            unresolved.extend(unsupported_aliases.get(eid, []))
            if eid not in accepted:
                unresolved.extend(rejected_names.get(eid, [f"unknown local ID: {eid}"]))
            elif accepted[eid]["identity_id"] is None:
                unresolved.append(accepted[eid]["names"][0])
        identities = list(dict.fromkeys(accepted[e]["identity_id"] for e in local_ids
                                       if e in accepted and accepted[e]["identity_id"] is not None))
        # Evidence survives an unresolved participant. It is an excerpt selection,
        # never proof that a predicted event actually happened (§0.2).
        records.append({**row, "source_chapter": case["chapter"], "passages": pids,
                        "entities": identities, "local_entities": [e for e in local_ids if e in accepted],
                        "unresolved": list(dict.fromkeys(unresolved)),
                        "evidence": [passages[p] for p in pids]})
    used = {e for row in records for e in row["local_entities"]}
    entities = [e for key, e in accepted.items() if key in used]
    # Spelling occurrences are retrieval/presentation candidates only (§5). They
    # deliberately carry no entity_id, even when two identities share a spelling.
    surfaces = sorted({n for entity in entities for n in entity["names"]})
    occurrences = [{"surface": n, "char_start": m.start(), "char_end": m.end()}
                   for n in surfaces for m in re.finditer(re.escape(n), case["source"])]
    return {"records": records, "entities": entities, "occurrence_candidates": occurrences,
            "diagnostics": diagnostics, "semantic_review": "pending", "published": False}


def gated_records(artifact, ids, at):
    if type(at) is not int or at < artifact["source_chapter"]:
        raise ValueError("reader chapter is below this evidence's knowledge chapter")
    records = {r["id"]: r for r in artifact["result"]["records"]}
    if not ids or len(ids) != len(set(ids)) or set(ids) - records.keys():
        raise ValueError("select explicit, unique evidence record IDs")
    return [records[i] for i in ids]


def search_source(artifact, query, at, limit=10):
    """Zero-completion lexical fallback over all chapter text, including unselected details."""
    if type(at) is not int or at < artifact["source_chapter"]:
        raise ValueError("reader chapter is below this evidence's knowledge chapter")
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or limit <= 0:
        raise ValueError("search requires a query and positive result limit")
    terms = set(query.casefold().split())
    hits = []
    for p in passages_for(artifact["case"]["source"]).values():
        score = sum(p["text"].casefold().count(t) for t in terms)
        if score:
            hits.append({**p, "source_chapter": artifact["source_chapter"], "score": score})
    return sorted(hits, key=lambda p: (-p["score"], p["id"]))[:limit]


def estimate_tokens(text):
    non_ascii = sum(ord(c) > 127 for c in text)
    return int(non_ascii * 1.3 + (len(text) - non_ascii) / 3) + 1


def enrichment_payload(records, artifact, naming_map, *, input_token_budget=None, include_context=True):
    """Attach bounded same-chapter context without changing selected evidence (§0.2–3).

    Nearby text can supply a speaker or qualification, never an identity binding.
    Context is optional: keep original evidence intact when the request is full.
    """
    local_ids = {i for r in records for i in r["local_entities"]}
    names = sorted({n for e in artifact["result"]["entities"] if e["id"] in local_ids for n in e["names"]})
    passages = passages_for(artifact["case"]["source"])
    payload = {"records": [{"id": r["id"], "topic": r["topic"], "passages": list(r["passages"]),
                            "context": []} for r in records],
               "passages": {str(p["id"]): p["text"] for r in records for p in r["evidence"]}}

    def with_names():
        text = "\n".join(payload["passages"].values())
        # A grounded unresolved surface may get a spelling, never an entity ID (§5).
        surfaces = {n for n in names if n in text} | {n for r in records for n in r["unresolved"] if n in text}
        return {**payload, "source_names": sorted(surfaces),
                "naming_map": {s: t for s, t in naming_map.items() if s in text}}

    base_size = estimate_tokens(wire(with_names()))
    if not include_context:
        return with_names()
    limit = base_size + artifact["policy"].get("max_context_tokens", 400)
    if input_token_budget is not None:
        limit = min(limit, input_token_budget)
    queues = []
    for record in records:
        selected = set(record["passages"])
        # Fill short gaps first, then immediate neighbors. Round-robin selection
        # below prevents the first record consuming the entire context allowance.
        ordered = sorted(selected)
        gap = [p for a, b in zip(ordered, ordered[1:]) if b - a <= 6 for p in range(a + 1, b)]
        neighbors = [p + delta for p in ordered for delta in (-1, 1)]
        queues.append(list(dict.fromkeys(p for p in gap + neighbors if p in passages and p not in selected)))
    for index in range(max(map(len, queues), default=0)):
        for record, candidates in zip(payload["records"], queues):
            if index >= len(candidates):
                continue
            pid = candidates[index]
            key = str(pid)
            already_present = key in payload["passages"]
            record["context"].append(pid)
            payload["passages"][key] = passages[pid]["text"]
            if estimate_tokens(wire(with_names())) > limit:
                record["context"].pop()
                if not already_present:
                    del payload["passages"][key]
    return with_names()


def validate_enrichment(text, payload):
    data = decode(text)
    ids = {r["id"] for r in payload["records"]}
    if (not isinstance(data, dict) or set(data) != {"notes", "names"}
            or not isinstance(data["notes"], dict) or set(data["notes"]) != ids
            or any(not isinstance(t, str) or not t.strip() for t in data["notes"].values())
            or not isinstance(data["names"], dict)
            or set(data["names"]) - set(payload["source_names"])
            or any(not isinstance(t, str) or not t.strip() for t in data["names"].values())):
        raise ValueError("invalid enrichment output")
    if any(s in payload["naming_map"] and t != payload["naming_map"][s] for s, t in data["names"].items()):
        raise ValueError("enrichment tried to change an existing spelling")
    if any(re.search(r"[⟦⟨]t\d+[⟧⟩]", t) for t in data["notes"].values()):
        raise ValueError("enrichment introduced placeholder markers")
    return {**data, "semantic_review": "pending", "status": "draft"}


def resolution_payload(records, artifact, candidate_limit):
    source = artifact["case"]["source"]
    references = [{"record": r["id"], "reference": s} for r in records for s in r["unresolved"] if s in source]
    if not references:
        raise ValueError("selected records have no source-grounded unresolved references")
    registry = [{"id": e["identity_id"], "names": e["names"], "kind": e["kind"]}
                for e in artifact["result"]["entities"] if e["identity_id"]]
    registry.extend(artifact["candidates"])
    candidates = retrieve("\n".join(r["reference"] for r in references), registry, candidate_limit)
    return {"references": references,
            "passages": {str(p["id"]): p["text"] for r in records for p in r["evidence"]},
            "candidates": list({e["id"]: e for e in candidates}.values())}


def validate_resolution(text, payload):
    data = decode(text)
    if not isinstance(data, dict) or set(data) != {"decisions"} or not isinstance(data["decisions"], list):
        raise ValueError("expected resolution decisions")
    expected = {(r["record"], r["reference"]) for r in payload["references"]}
    seen, diagnostics, checked = set(), [], []
    candidates = {e["id"] for e in payload["candidates"]}
    passages = {int(p): t for p, t in payload["passages"].items()}
    for row in data["decisions"]:
        if not isinstance(row, dict) or set(row) != {"record", "reference", "candidate", "passages"}:
            raise ValueError("invalid resolution decision")
        if not isinstance(row["record"], str) or not isinstance(row["reference"], str):
            raise ValueError("invalid resolution reference")
        key = row["record"], row["reference"]
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate resolution decision")
        seen.add(key)
        if row["candidate"] is not None and (not isinstance(row["candidate"], str) or row["candidate"] not in candidates):
            raise ValueError("resolution links unknown identity")
        pids = passage_ids(row["passages"], passages, diagnostics, row["record"])
        if not any(row["reference"] in passages[p] for p in pids):
            raise ValueError("resolution reference has no cited witness")
        checked.append({**row, "passages": pids})
    if seen != expected:
        raise ValueError("resolution omitted references")
    return {"decisions": checked, "diagnostics": diagnostics, "semantic_review": "pending", "status": "draft"}
