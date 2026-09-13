"""Reusable pinned fact-first extraction core and benchmark adapter.

The core owns discovery, compact selection, normalization, validation, and optional
translation. Production workers persist its stage artifacts; the benchmark can replay
the same requests and model responses without touching request-path services (§0).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Annotated, Literal
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from pipeline.config import Config
from pipeline.llm import provider_from_env
from pipeline.llm.provider import Class, Completion
from pipeline.benchmark_memory import (
    DISCOVERY_SYSTEM as MEMORY_DISCOVERY_SYSTEM, MemoryMetadata,
    candidates as memory_candidates,
)
from pipeline.benchmark_selection import (
    NORMALIZATION_SELECTION_SYSTEM, selected_discovery, selection_request, validate_selection,
)


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
PassageId = Annotated[str, StringConstraints(pattern=r"^p[0-9]{3,}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimProposal(StrictModel):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    claim_source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    evidence_ids: list[PassageId] = Field(min_length=1, max_length=4)
    context_ids: list[PassageId] = Field(max_length=4)


class MemoryClaimProposal(ClaimProposal):
    memory: MemoryMetadata


# Per-variant claim ceilings. The baseline prompt's 30 is kept verbatim for comparison;
# support_first is recall-first, and on Chapter 1 (56 passages) gpt-oss-120b returned 45
# claims against a 30 cap -- a ceiling that low silently re-imposes the importance
# filter the variant exists to remove. The schema admits the largest ceiling; the
# variant's own ceiling is enforced in validate_discovery.
# atomic splits compound claims, multiplying them; 60 would re-impose selection.
CLAIM_CEILINGS = {"baseline": 30, "revised": 60, "support_first": 60, "memory": 30, "atomic": 80}
MAX_CLAIM_CEILING = max(CLAIM_CEILINGS.values())


class DiscoveryResponse(StrictModel):
    claims: list[ClaimProposal] = Field(max_length=MAX_CLAIM_CEILING)


class ClaimDecision(StrictModel):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    verdict: Literal["supported", "contradicted", "insufficient_evidence"]
    evidence_ids: list[PassageId] = Field(max_length=4)


class EntityProposal(StrictModel):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    # IDs are opaque labels, consistent only within one response; nemotron-3-ultra
    # zero-pads them (e001), which is harmless. An inconsistent reference still fails
    # closed as an unknown entity.
    local_id: Annotated[str, StringConstraints(pattern=r"^e[0-9]{1,4}$")]
    canonical_source: NonEmpty
    source_aliases: list[NonEmpty] = Field(max_length=12)
    english_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    kind: Identifier
    # No evidence_ids: an entity's evidence is derived by the validator (the passages
    # in its claim's package that literally contain one of its names), not chosen by
    # the model. Model-chosen entity citations only ever added ways to fail -- citing
    # every mention (7 > cap) or a passage from another claim's package.


AssertionId = Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*\.a[1-9][0-9]*$")]
ASSERTION_ATTRS = ("assertion", "source", "polarity", "attribution", "condition", "temporal")


class AssertionFields(StrictModel):
    """The explicit meaning of one record, identical across variants (item 2).

    ``source_span`` is the source wording and must be verbatim in the record's evidence;
    a fact's ``value`` is a normalized rendering and need not be. Whether the rendering
    matches the span is a semantic question for the verifier, never a string check.
    Optional here so saved variants without these fields still validate.
    """
    assertion_id: AssertionId | None = None
    source_span: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None = None
    polarity: Literal["affirmed", "negated"] | None = None
    attribution: NonEmpty | None = None
    condition: Literal["none", "conditional", "possible", "offered"] | None = None
    temporal: Literal["prior", "during", "planned"] | None = None


class AssertionAccount(StrictModel):
    """What happened to each assertion a supported claim makes (item 5)."""
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    assertion_id: AssertionId
    statement: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    outcome: Literal["represented", "unresolved", "omitted"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""


class FactProposal(AssertionFields):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    subject_id: NonEmpty
    attribute: Identifier
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    evidence_ids: list[PassageId] = Field(min_length=1, max_length=4)


class RelationProposal(AssertionFields):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    src_id: NonEmpty
    dst_id: NonEmpty
    relation: Identifier
    evidence_ids: list[PassageId] = Field(min_length=1, max_length=4)


class EventArgument(StrictModel):
    """``role:ID`` names a participant; ``role=value`` is a literal copied from evidence.

    Before literals existed, every model that followed the prompt's request for speech,
    durations and destinations wrote ``duration:two_hours`` and lost the whole event.
    """
    role: Identifier
    entity_id: NonEmpty | None = None
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "EventArgument":
        if (self.entity_id is None) == (self.value is None):
            raise ValueError("event argument needs exactly one of entity_id or value")
        return self


class ReferenceProposal(StrictModel):
    """A surface whose identity the passage alone does not fix, left for RESOLVE.

    ``candidate_id`` is a hint and is never merged; a reference never enters the
    English name map, so it cannot leak a guessed identity into translation.
    """
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    local_id: Annotated[str, StringConstraints(pattern=r"^r[0-9]{1,4}$")]
    surface: NonEmpty
    refers_to: Identifier
    candidate_id: Annotated[str, StringConstraints(pattern=r"^e[0-9]{1,4}$")] | None = None


class EventProposal(AssertionFields):
    claim_id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    action: Identifier
    arguments: list[EventArgument] = Field(max_length=16)
    evidence_ids: list[PassageId] = Field(min_length=1, max_length=4)


class ExtractionResponse(StrictModel):
    decisions: list[ClaimDecision] = Field(max_length=MAX_CLAIM_CEILING)
    entities: list[EntityProposal] = Field(max_length=80)
    facts: list[FactProposal] = Field(max_length=80)
    relations: list[RelationProposal] = Field(max_length=40)
    events: list[EventProposal] = Field(max_length=50)


DISCOVERY_SYSTEM = """\
Read one Chinese web-novel chapter and discover only explicit claims worth retaining in
the chapter's knowledge graph. Select claims that materially explain the chapter's
conflict, character goals or decisions, relationships, discoveries, consequential
changes, or enduring character/world knowledge. Include an ability, item, or world rule
only when the passage establishes it as a meaningful capability, constraint, revelation,
or explanation of an outcome. Do not predict future importance from genre conventions.

Omit atmosphere, visual flourish, temporary effects, minor gestures, routine movement,
repeated travel progress, and stated intentions that do not occur. A supported detail is
not automatically a graph claim: do not extract a momentary description as an attribute
or ability merely because it is true. Return fewer claims when the chapter establishes
little consequential knowledge; the maximum is not a target. Express each selected claim
as one short,
self-contained Chinese proposition that preserves who did what to whom, where, and with
what outcome. Replace ambiguous pronouns or omitted subjects with names only when the
cited context makes the identity clear. Cite direct support with evidence_ids. Add
context_ids only when needed for identity or omitted context. Do not classify graph
types, choose English names, or copy passage text. Return at most 30 claims as XML and
no prose. Use exactly this shape, with comma-separated passage IDs:
<claims><claim id="c1" evidence="p001" context="">完整的中文命题</claim></claims>
"""

# The baseline prompt above is intentionally retained verbatim for comparison.  The
# revised policy changes only selection: it records explicit source-grounded claims
# without asking the model to predict importance or consequence.
DISCOVERY_SUPPORT_FIRST_SYSTEM = """\
Read one Chinese web-novel chapter and list the explicit, source-grounded claims that
the chapter establishes. Include identities, descriptions and properties,
relationships, events or actions, changes, and explicitly attributed statements,
beliefs, intentions, or plans. Include a claim when the cited passage states it, even
when it is temporary, incidental, a gesture, movement, or an intention that has not yet
been carried out. Do not filter claims by narrative significance, permanence, or
anticipated future use.

Keep claims concise, self-contained, and nonduplicate. Preserve who did what to whom,
where, and with what stated outcome. Distinguish an accomplished event from an
intention or plan, and distinguish a character's assertion or belief from an established
fact. Do not turn figurative language into a literal ability, event, or property, and do
not infer an unstated outcome. Replace ambiguous pronouns or omitted subjects with
names only when the cited context makes the identity clear. Cite direct support with
evidence_ids. Add context_ids only when needed for identity or omitted context. Do not
classify graph types, choose English names, or copy passage text. Return at most 60
claims as XML and no prose. Use exactly this shape, with comma-separated passage IDs:
<claims><claim id="c1" evidence="p001" context="">完整的中文命题</claim></claims>
"""


NORMALIZATION_SYSTEM = """\
Verify each raw Chinese claim against its supplied exact evidence and context, then
normalize only supported knowledge into compact entities, durable facts, relations, and
role-labelled events. Use no knowledge outside the supplied evidence package.

Return exactly one decision for every supplied claim. The decision claim ID must be the
original claim ID, and verdict must be supported, contradicted, or insufficient_evidence.
Use evidence IDs from the supplied passage package only. A contradicted or
insufficient_evidence claim must not produce graph records. Every graph record must
carry the original claim ID that caused it.

Group source aliases that clearly name the same subject under one local_id. Do not group
subjects merely because their spellings or titles are similar. Generic nouns, pronouns,
kinship terms, and unanchored titles are not entities. When unsure, omit the alias or
entity. Use the allowed entity kinds supplied in the input.

Facts are durable properties that remain true after the immediate scene. Current actions,
positions, weather, and scene beats belong in events. Relations require two named
subjects. Represent event arguments as a comma-separated list of arbitrary
snake_case_role:entity_id pairs in an args attribute. Reject claims that the evidence does not support. Every proposal must cite
one or more supplied passage IDs in evidence_ids. Never copy Chinese passage text into
the response. Use short snake_case action, attribute, and relation names. English names
are renderings, never evidence. Return XML and no prose. The root must be knowledge with
exactly decisions, entities, facts, relations, and events children. Use empty children
when no claim is supported. Attribute rules: claim IDs begin with c, entity IDs begin
with e, passage IDs begin with p, evidence is a comma-separated passage-ID list, aliases
is a comma-separated source-name list, and event args is a comma-separated role:entity
list. The following signatures document the required attributes; uppercase metavariables
are documentation only and must never be emitted as literal values:
<decision claim="CLAIM_ID" verdict="VERDICT" evidence="PASSAGE_IDS"/>
<entity claim="CLAIM_ID" id="ENTITY_ID" source="SOURCE_NAME" aliases="SOURCE_ALIASES" english="ENGLISH_NAME" kind="ENTITY_KIND" evidence="PASSAGE_IDS"/>
<fact claim="CLAIM_ID" subject="ENTITY_ID" attribute="ATTRIBUTE" value="SOURCE_VALUE" evidence="PASSAGE_IDS"/>
<relation claim="CLAIM_ID" src="ENTITY_ID" dst="ENTITY_ID" type="RELATION" evidence="PASSAGE_IDS"/>
<event claim="CLAIM_ID" action="ACTION" args="ROLE:ENTITY_ID,..." evidence="PASSAGE_IDS"/>
Structural skeleton:
<knowledge><decisions></decisions><entities></entities><facts></facts><relations></relations><events></events></knowledge>
"""

NORMALIZATION_SUPPORT_FIRST_SYSTEM = """\
Verify each Chinese claim using only its evidence_ids and context_ids. Context includes
nearby passages for resolving pronouns; proximity alone does not establish identity.
Return exactly one decision per claim: supported, contradicted, or insufficient_evidence.
A supported claim is not rejected for being incidental or temporary. Only supported
claims may produce records; every record retains its originating claim ID.

Build a consistent entity inventory. Reuse IDs across claims; include explicitly
established short names and aliases, never merge by spelling alone. Cite the supported
claim that introduces each entity. Use only supplied ontology kinds.
Do not substitute a nearby person or weapon for the actual subject of a property.
Check every participant against the claim and source, including pronoun antecedents.

The fact-versus-event distinction concerns representation: facts describe properties;
events describe occurrences. Preserve intentions and attributed speech as such, never
as accomplished actions. Split compound claims into separate records for each supported
assertion. Event args accept only role:entity_id pairs, never literal content, duration,
or direction. If the schema cannot preserve an assertion, omit that record and retain
the supported decision; never invent an entity or assign a substitute subject.

Return XML only. Include each section once, empty if unused:
<knowledge><decisions/><entities/><facts/><relations/><events/></knowledge>
Use these exact row attributes (uppercase values below are placeholders):
<decision claim="CLAIM_ID" verdict="VERDICT" evidence="PASSAGE_IDS"/>
<entity claim="CLAIM_ID" id="ENTITY_ID" source="SOURCE_NAME" aliases="SOURCE_ALIASES" english="ENGLISH_NAME" kind="ENTITY_KIND"/>
<fact claim="CLAIM_ID" subject="ENTITY_ID" attribute="ATTRIBUTE" value="VALUE" evidence="PASSAGE_IDS"/>
<relation claim="CLAIM_ID" src="ENTITY_ID" dst="ENTITY_ID" type="RELATION" evidence="PASSAGE_IDS"/>
<event claim="CLAIM_ID" action="ACTION" args="ROLE:ENTITY_ID,..." evidence="PASSAGE_IDS"/>
IDs use c/e/p prefixes; evidence and aliases are comma-separated lists. Evidence must
belong to that claim's package. Use short ASCII snake_case attributes, actions, relations
and roles. English names are renderings, never evidence. Escape XML attribute values.
"""
# Step 4: the support_first policy with a schema that can hold what it asks for. The
# step-0 audit found 62 rows lost to literal event arguments and 48 to short names or
# titles; the step-1 regressions found 5/7 non-person entities typed as characters.
# Only representation changes here -- verification is a separate stage (step 2).
NORMALIZATION_REPRESENTATION_SYSTEM = NORMALIZATION_SUPPORT_FIRST_SYSTEM.replace(
    "Event args accept only role:entity_id pairs, never literal content, duration,\n"
    "or direction.",
    "Event args are role:ID pairs for entities or references, or role=value literals\n"
    "copied verbatim from the cited evidence (duration, destination, quantity, speech).",
).replace(
    "Use only supplied ontology kinds.",
    "Use only supplied ontology kinds and their descriptions. Classify each referent\n"
    "according to its role in this text; do not force it into a person kind or infer\n"
    "its kind from genre conventions. Leave it unresolved when no kind fits.",
).replace(
    "If the schema cannot preserve an assertion, omit that record and retain\n"
    "the supported decision; never invent an entity or assign a substitute subject.",
    "If the schema cannot preserve an assertion, omit that record and retain\n"
    "the supported decision; never invent an entity or assign a substitute subject.\n"
    "When a collective noun, title or short form names someone the passage alone does\n"
    "not identify, emit a reference with that exact surface instead of an entity;\n"
    "candidate may name a likely entity ID as an unmerged hint. Records may cite r IDs.",
).replace(
    "<knowledge><decisions/><entities/><facts/><relations/><events/></knowledge>",
    "<knowledge><decisions/><entities/><references/><facts/><relations/><events/></knowledge>",
).replace(
    '<fact claim="CLAIM_ID"',
    '<reference claim="CLAIM_ID" id="REF_ID" surface="EXACT_SOURCE_SURFACE" refers_to="KIND_OR_unknown" candidate="ENTITY_ID_OR_EMPTY"/>\n'
    '<fact claim="CLAIM_ID"',
).replace(
    'args="ROLE:ENTITY_ID,..."', 'args="ROLE:ENTITY_OR_REF_ID,ROLE=LITERAL,..."',
).replace("IDs use c/e/p prefixes", "IDs use c/e/r/p prefixes")
assert NORMALIZATION_REPRESENTATION_SYSTEM.count("reference") >= 3, "prompt splice drifted"
NORMALIZATION_VARIANTS = ("baseline", "revised", "support_first", "memory", "representation")

# Item 5: recall-first discovery with atomic claims. The missing-links audit found
# kinship and a title dropped inside compound claims (K04, K09), an explicitly stated
# ignorance never extracted by any variant (K06), and a pronoun subject lost (K14).
DISCOVERY_ATOMIC_SYSTEM = DISCOVERY_SUPPORT_FIRST_SYSTEM.replace(
    "Keep claims concise, self-contained, and nonduplicate.",
    "Keep claims concise, self-contained, and nonduplicate. Make each claim one assertion:\n"
    "relationships, roles, properties and actions are separate claims even\n"
    "when one sentence states them together. Include explicitly stated knowledge, ignorance\n"
    "and belief (who knows, does not know, believes or suspects what) only when the text\n"
    "states it; never infer ignorance from silence. When the subject is a pronoun such as\n"
    "他 or 我们, name the referent only if cited context fixes it; otherwise keep the pronoun.",
)
assert DISCOVERY_ATOMIC_SYSTEM != DISCOVERY_SUPPORT_FIRST_SYSTEM, "prompt splice drifted"

# Items 2 and 5: the working baseline (representation) plus an explicit, identical
# assertion contract on every record, explicit event-argument fields, and per-assertion
# accounting so one record can never silently stand in for two assertions.
NORMALIZATION_ASSERTION_SYSTEM = """\
Verify each Chinese claim using only its evidence_ids and context_ids. Context includes
nearby passages for resolving pronouns; proximity alone does not establish identity.
Return exactly one decision per claim: supported, contradicted, or insufficient_evidence.
A supported claim is not rejected for being incidental or temporary.

Account for every assertion a supported claim makes. When a claim joins several
assertions (a relationship and an action, a role and a belief), list each as
its own assertion row with its own records. An assertion's outcome is represented
(records carry its ID), unresolved (the schema or an identity cannot hold it yet, with a
reason) or omitted (with a reason). One record never stands for two assertions.

Build a consistent entity inventory: reuse IDs and explicitly established short names;
never merge by spelling alone; each entity cites the claim introducing it and uses a
supplied ontology kind using its description and this text. Never force a referent
into a person kind or infer its kind from genre conventions. When a pronoun, collective
noun, title or short form names someone the
passage alone does not identify, emit a reference with that exact surface; candidate may
name a likely entity ID as an unmerged hint. Records may cite r IDs.

Every fact, relation and event states its meaning explicitly:
source: the exact words that state it, copied from one cited passage;
value (facts only): a normalized rendering, which need not appear in the source;
polarity: affirmed or negated;
attribution: narrator, or the e/r ID of whoever says, believes or plans it;
condition: none, conditional, possible or offered;
temporal: prior (before this chapter), during, or planned.
An offer is not a promise, "may consider" is conditional, and one action is not a
capability. Event arguments are child elements: <arg role="ROLE" entity="ID"/> for a
participant, <arg role="ROLE" literal="EXACT SOURCE WORDS"/> for a value.

Return XML only. Include each section once, empty if unused:
<knowledge><decisions/><accounting/><entities/><references/><facts/><relations/><events/></knowledge>
Rows (uppercase values are placeholders):
<decision claim="CLAIM_ID" verdict="VERDICT" evidence="PASSAGE_IDS"/>
<assertion claim="CLAIM_ID" id="CLAIM_ID.aN" statement="SHORT_CHINESE_ASSERTION" outcome="represented" reason=""/>
<entity claim="CLAIM_ID" id="ENTITY_ID" source="SOURCE_NAME" aliases="SOURCE_ALIASES" english="ENGLISH_NAME" kind="ENTITY_KIND"/>
<reference claim="CLAIM_ID" id="REF_ID" surface="EXACT_SOURCE_SURFACE" refers_to="KIND_OR_unknown" candidate="ENTITY_ID_OR_EMPTY"/>
<fact claim="CLAIM_ID" assertion="ASSERTION_ID" subject="ID" attribute="ATTRIBUTE" value="VALUE" source="EXACT_SOURCE_WORDS" polarity="affirmed" attribution="narrator" condition="none" temporal="during" evidence="PASSAGE_IDS"/>
<relation claim="CLAIM_ID" assertion="ASSERTION_ID" src="ID" dst="ID" type="RELATION" source="EXACT_SOURCE_WORDS" polarity="affirmed" attribution="narrator" condition="none" temporal="during" evidence="PASSAGE_IDS"/>
<event claim="CLAIM_ID" assertion="ASSERTION_ID" action="ACTION" source="EXACT_SOURCE_WORDS" polarity="affirmed" attribution="narrator" condition="none" temporal="during" evidence="PASSAGE_IDS"><arg role="agent" entity="e1"/></event>
IDs use c/e/r/p prefixes; evidence and aliases are comma-separated. Evidence must belong
to that claim's package. Use short ASCII snake_case attributes, actions, relations and
roles. English names are renderings, never evidence. Escape XML attribute values.
"""
NORMALIZATION_VARIANTS = (*NORMALIZATION_VARIANTS, "assertion")

DISCOVERY_REVISED_SYSTEM = DISCOVERY_SUPPORT_FIRST_SYSTEM
NORMALIZATION_REVISED_SYSTEM = NORMALIZATION_SUPPORT_FIRST_SYSTEM
NORMALIZATION_MEMORY_SYSTEM = NORMALIZATION_SUPPORT_FIRST_SYSTEM.replace(
    "Verify each Chinese claim using only its evidence_ids and context_ids.",
    "Check each proposition AND memory field against its evidence/context. Audit actor,\n"
    "beneficiary, condition and attribution; a commander is not the person obeying.\n"
    "Reject unsupported certainty, mechanisms or qualifiers even if the main topic is correct.",
).replace(
    "The fact-versus-event distinction concerns representation: facts describe properties;\n"
    "events describe occurrences.",
    "Wiki entries describe properties; storyline entries describe objective, plan, obstacle\n"
    "or outcome. Keep these scopes; do not expand them into scene-by-scene actions.",
).replace(
    "Split compound claims into separate records for each supported\nassertion.",
    "One comparison unit per assertion; separate crimes and different subjects.\n"
    "Do not flatten a conditional offer into a promise. First observation is not acquisition.",
).replace(
    "Build a consistent entity inventory. Reuse IDs across claims; include explicitly\n"
    "established short names and aliases, never merge by spelling alone. Cite the supported\n"
    "claim that introduces each entity. Use only supplied ontology kinds.\n"
    "Do not substitute a nearby person or weapon for the actual subject of a property.\n"
    "Check every participant against the claim and source, including pronoun antecedents.",
    "Reuse IDs and established aliases. source must be the actual name, never a placeholder.\n"
    "Each entity cites the claim that introduces it and uses a supplied ontology kind.\n"
    "Verify pronoun antecedents; never substitute nearby subjects or merge by spelling alone.",
)


TRANSLATION_SYSTEM = """\
Translate one Chinese web-novel chapter into natural English. Preserve all non-empty
paragraphs in their original order and preserve paragraph breaks. Use every supplied
source-to-English proper-name mapping consistently. Do not summarize, omit, add, or
explain anything. Return XML and no prose, with exactly one output element for every
input passage in the same order: <translation><p id="p001">English text</p></translation>.
"""


def _digest(value: Any) -> str:
    material = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode()).hexdigest()


def load_case(dataset_path: str | Path, chapter: int) -> dict[str, Any]:
    dataset = json.loads(Path(dataset_path).read_text())
    row = next((item for item in dataset.get("chapters", []) if item.get("chapter") == chapter), None)
    if not row or not isinstance(row.get("source"), str) or not row["source"].strip():
        raise ValueError(f"chapter {chapter} is absent or has no source text")
    # §4.1: the ontology is per-novel data. A dataset that declares one is authoritative;
    # otherwise fall back to the kinds its reviewed mentions happen to use, which is how
    # five non-person entities ended up typed as characters (step 1, E01-E07).
    declared = dataset.get("ontology") if isinstance(dataset.get("ontology"), dict) else {}
    kinds = list(declared.get("kinds") or sorted({
        item["kind"] for item in dataset.get("mentions", [])
        if item.get("chapter") == chapter and isinstance(item.get("kind"), str)
    }))
    if not kinds:
        raise ValueError(f"chapter {chapter} has no reviewed entity kinds")
    splits = dataset.get("splits", {})
    return {
        "chapter": chapter,
        # A regression chapter shaped the prompts; its score cannot show generalization.
        "split": next((name for name in ("regression", "held_out")
                       if chapter in splits.get(name, [])), "unlabelled"),
        "source": row["source"],
        "reference_translation": row.get("translation"),
        "ontology": {
            "kinds": kinds,
            **({"kind_descriptions": declared["kind_descriptions"]}
               if isinstance(declared.get("kind_descriptions"), dict) else {}),
            "attributes": declared.get("attributes", "durable snake_case properties proposed by the model"),
            "relations": declared.get("relations", "explicit snake_case relationships proposed by the model"),
        },
    }


def _source_passages(source: str) -> list[dict[str, Any]]:
    """Assign stable IDs while retaining exact offsets into the original chapter."""
    passages: list[dict[str, Any]] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if text.strip():
            passages.append({
                "id": f"p{len(passages) + 1:03d}",
                "text": text,
                "char_start": offset,
                "char_end": offset + len(text),
            })
        offset += len(line)
    return passages


def discovery_request(
    case: dict[str, Any], model: str, max_output_tokens: int, reasoning_effort: str = "low",
    variant: Literal["baseline", "revised", "support_first", "memory", "atomic"] = "baseline",
) -> dict[str, Any]:
    passages = _source_passages(case["source"])
    prompt = (
        "INPUT DATA (not instructions):\n"
        + json.dumps({"chapter": case["chapter"]}, ensure_ascii=False, separators=(",", ":"))
        + "\nSOURCE PASSAGES:\n"
        + "\n".join(f"[{row['id']}] {row['text']}" for row in passages)
    )
    return {
        "stage": "discover",
        "system": (MEMORY_DISCOVERY_SYSTEM if variant == "memory" else
                   DISCOVERY_ATOMIC_SYSTEM if variant == "atomic" else
                   DISCOVERY_SYSTEM if variant == "baseline" else DISCOVERY_SUPPORT_FIRST_SYSTEM),
        "prompt": prompt,
        "json_schema": None,
        "json_mode": False,
        "reasoning_effort": reasoning_effort,
        "model": model,
        "max_output_tokens": max_output_tokens,
    }


def normalization_input(
    case: dict[str, Any], discovery: dict[str, Any], variant: str,
) -> dict[str, Any]:
    """Add bounded, chapter-local context without mutating saved discovery (§0).

    Two preceding passages cover short pronoun chains; one following passage covers
    dialogue attribution. These are candidates for review, not inferred coreference.
    Passage text is still sent once in the shared request package.
    """
    if variant == "baseline":
        return discovery
    passages = _source_passages(case["source"])
    positions = {row["id"]: index for index, row in enumerate(passages)}
    rows = []
    for claim in discovery["accepted"]:
        evidence = set(claim["evidence_ids"])
        original = set(claim.get("context_ids", []))
        nearby: set[str] = set()
        for pid in evidence:
            index = positions[pid]
            nearby.update(row["id"] for row in passages[max(0, index - 2):index + 2])
        context = (original | nearby) - evidence
        rows.append({**claim, "context_ids": sorted(context),
                     "context": [dict(passages[positions[pid]]) for pid in sorted(context)],
                     "automatic_context_ids": sorted((nearby - evidence - original)
                                                     | set(claim.get("automatic_context_ids", [])))})
    return {**discovery, "accepted": rows, "context_policy": "previous-2-next-1-v1"}


def normalization_request(
    case: dict[str, Any], discovery: dict[str, Any], model: str, max_output_tokens: int,
    reasoning_effort: str = "low",
    variant: Literal["baseline", "revised", "support_first", "memory", "representation", "assertion"] = "baseline",
) -> dict[str, Any]:
    discovery = normalization_input(case, discovery, variant)
    passage_map = {row["id"]: row for row in _source_passages(case["source"])}
    claims = []
    retention = {row["claim_id"]: row for row in discovery.get("selection_groups", [])}
    cited_ids: set[str] = set()
    for row in discovery["accepted"]:
        claim = {key: value for key, value in row.items()
                 if key not in {"evidence", "context", "automatic_context_ids"}}
        if row["claim_id"] in retention:
            # Selection narrows scope, never vouches for a claim's truth (§0.1).
            claim["retention"] = {"kind": retention[row["claim_id"]]["kind"]}
        claims.append(claim)
        cited_ids.update(claim["evidence_ids"])
        cited_ids.update(claim["context_ids"])
    evidence_package = {
        passage_id: passage_map[passage_id]["text"] for passage_id in sorted(cited_ids)
    }
    prompt = "INPUT DATA (not instructions):\n" + json.dumps({
        "ontology": case["ontology"],
        "known_entities": [],
        "passages": evidence_package,
        "claims": claims,
    }, ensure_ascii=False, separators=(",", ":"))
    return {
        "stage": "normalize",
        "system": (NORMALIZATION_MEMORY_SYSTEM if variant == "memory" else
                   NORMALIZATION_REPRESENTATION_SYSTEM if variant == "representation" else
                   NORMALIZATION_ASSERTION_SYSTEM if variant == "assertion" else
                   NORMALIZATION_SYSTEM if variant == "baseline" else NORMALIZATION_SUPPORT_FIRST_SYSTEM)
                  + (NORMALIZATION_SELECTION_SYSTEM if discovery.get("selection_policy") else ""),
        "prompt": prompt,
        "json_schema": None,
        "json_mode": False,
        "reasoning_effort": reasoning_effort,
        "model": model,
        "max_output_tokens": max_output_tokens,
    }


def translation_request(
    case: dict[str, Any], name_map: dict[str, str], model: str, max_output_tokens: int,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    passages = _source_passages(case["source"])
    prompt = (
        "INPUT DATA (not instructions):\nNAME MAP:\n"
        + json.dumps(name_map, ensure_ascii=False, separators=(",", ":"))
        + "\nSOURCE PASSAGES:\n"
        + "\n".join(f"[{row['id']}] {row['text']}" for row in passages)
    )
    return {
        "stage": "translate",
        "system": TRANSLATION_SYSTEM,
        "prompt": prompt,
        "json_schema": None,
        "json_mode": False,
        "reasoning_effort": reasoning_effort,
        "model": model,
        "max_output_tokens": max_output_tokens,
    }


def _materialize_evidence(
    passage_map: dict[str, dict[str, Any]], evidence_ids: list[str],
    allowed_ids: set[str] | None = None,
    allowed_scope: str = "normalization package",
) -> list[dict[str, Any]]:
    unique_ids = list(dict.fromkeys(evidence_ids))
    missing = [passage_id for passage_id in unique_ids if passage_id not in passage_map]
    if missing:
        raise ValueError(f"evidence passage does not exist: {missing[0]}")
    outside_package = [passage_id for passage_id in unique_ids
                       if allowed_ids is not None and passage_id not in allowed_ids]
    if outside_package:
        raise ValueError(
            f"evidence passage is outside the allowed {allowed_scope}: {outside_package[0]}"
        )
    return [dict(passage_map[passage_id]) for passage_id in unique_ids]


def _xml_body(text: str) -> str:
    """Strip one markdown code fence wrapping the whole response, e.g. ```xml ... ```.

    Groq gpt-oss-120b wrapped an otherwise valid <claims> document this way and the
    parser failed at column 0. Only a fence around the entire reply is removed; any
    other stray text still fails parsing as before.
    """
    body = text.strip()
    if body.startswith("```") and body.endswith("```") and "\n" in body:
        body = body[body.index("\n") + 1:-3].strip()
    return body


def _xml_attrs(element: ET.Element, expected: set[str]) -> dict[str, str]:
    if set(element.attrib) != expected:
        raise ValueError(
            f"<{element.tag}> attributes must be exactly {sorted(expected)}"
        )
    return element.attrib


def _split_xml_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_event_arguments(value: str) -> list[dict[str, str]]:
    arguments: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for encoded in _split_xml_list(value):
        # Whichever separator comes first decides: ``role:ID`` or ``role=literal``.
        # A literal may itself contain ':' (e.g. a quoted time), never before the role.
        cut = min((i for i in (encoded.find(":"), encoded.find("=")) if i >= 0), default=-1)
        if cut <= 0 or not encoded[cut + 1:].strip():
            raise ValueError("event args must be comma-separated role:ID or role=value pairs")
        role, target = encoded[:cut].strip(), encoded[cut + 1:].strip()
        key = "value" if encoded[cut] == "=" else "entity_id"
        pair = (role, target)
        if pair in seen_pairs:
            raise ValueError(f"event args contain duplicate role/target pair: {role}:{target}")
        seen_pairs.add(pair)
        arguments.append({"role": role, key: target})
    return arguments


def _parse_discovery_xml(
    text: str, variant: str = "baseline",
) -> tuple[list[tuple[int, ClaimProposal]], int, list[dict[str, Any]]]:
    """Parse ``<claims>``, dropping one malformed ``<claim>`` rather than the batch.

    A single bad attribute on one of 50+ claims (a stray passage ID copied into
    ``id``, a missing ``context=""``) used to fail the whole discovery response and
    lose every other, well-formed claim with it. Only the document-level shape
    (root tag, no root attributes) is still fatal; each ``<claim>`` is judged on its
    own. Returns the surviving claims paired with their original position (so later
    evidence-validation rejections can still report where they came from), the total
    number of ``<claim>`` tags seen (the model's real output size, for the ceiling
    check), and the tags dropped here with why.
    """
    root = ET.fromstring(_xml_body(text))
    if root.tag != "claims" or root.attrib:
        raise ValueError("discovery XML root must be <claims> without attributes")
    claims: list[tuple[int, ClaimProposal]] = []
    dropped: list[dict[str, Any]] = []
    for index, child in enumerate(root):
        try:
            if child.tag != "claim" or list(child):
                raise ValueError("<claims> may contain only text-only <claim> elements")
            fields = set(MemoryMetadata.model_fields) if variant == "memory" else set()
            attrs = _xml_attrs(child, {"id", "evidence", "context"} | fields)
            row = {
                "claim_id": attrs["id"],
                "claim_source": (child.text or "").strip(),
                "evidence_ids": _split_xml_list(attrs["evidence"]),
                "context_ids": _split_xml_list(attrs["context"]),
            }
            proposal = ClaimProposal.model_validate(row)
            if variant == "memory":
                metadata = MemoryMetadata.model_validate({key: attrs[key] for key in fields})
                # Keep the closed old discovery contract unchanged for saved baselines.
                proposal = MemoryClaimProposal(**proposal.model_dump(), memory=metadata)
            claims.append((index, proposal))
        except (ValueError, ValidationError) as exc:
            dropped.append({"index": index, "tag": child.tag, "attrib": dict(child.attrib),
                            "reason": str(exc)})
    return claims, len(root), dropped


def _parse_normalization_row_document(text: str) -> dict[str, list[dict[str, Any]]]:
    root = ET.fromstring(_xml_body(text))
    expected_sections = {"decisions", "entities", "facts", "relations", "events"}
    if root.tag != "knowledge" or root.attrib:
        raise ValueError("normalization XML root must be <knowledge> without attributes")
    if len(root) != 5 or {child.tag for child in root} != expected_sections:
        raise ValueError(
            "<knowledge> must contain decisions, entities, facts, relations, and events once"
        )
    sections = {child.tag: child for child in root}
    body: dict[str, list[dict[str, Any]]] = {name: [] for name in expected_sections}

    for child in sections["decisions"]:
        if child.tag != "decision" or list(child):
            raise ValueError("<decisions> may contain only <decision> elements")
        attrs = _xml_attrs(child, {"claim", "verdict", "evidence"})
        body["decisions"].append({
            "claim_id": attrs["claim"],
            "verdict": attrs["verdict"],
            "evidence_ids": _split_xml_list(attrs["evidence"]),
        })
    for child in sections["entities"]:
        if child.tag != "entity" or list(child):
            raise ValueError("<entities> may contain only <entity> elements")
        # An `evidence` attribute is tolerated (baseline prompts and saved responses
        # still emit it) but ignored: entity evidence is derived in validate_extraction.
        entity_attrs = {"claim", "id", "source", "aliases", "english", "kind"}
        attrs = _xml_attrs(
            child, entity_attrs | {"evidence"} if "evidence" in child.attrib else entity_attrs
        )
        body["entities"].append({
            "claim_id": attrs["claim"],
            "local_id": attrs["id"], "canonical_source": attrs["source"],
            "source_aliases": _split_xml_list(attrs["aliases"]),
            "english_name": attrs["english"], "kind": attrs["kind"],
        })
    def assertion_attrs(child: ET.Element, base: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
        # All-or-none: a record either states its whole meaning (assertion variant) or,
        # as in every saved variant, none of it. A partial set is a malformed row.
        extra = set(ASSERTION_ATTRS) if set(ASSERTION_ATTRS) & set(child.attrib) else set()
        attrs = _xml_attrs(child, base | extra)
        fields = {"assertion_id": attrs["assertion"], "source_span": attrs["source"],
                  "polarity": attrs["polarity"], "attribution": attrs["attribution"],
                  "condition": attrs["condition"], "temporal": attrs["temporal"]} if extra else {}
        return attrs, fields

    for child in sections["facts"]:
        if child.tag != "fact" or list(child):
            raise ValueError("<facts> may contain only <fact> elements")
        attrs, fields = assertion_attrs(child, {"claim", "subject", "attribute", "value", "evidence"})
        body["facts"].append({
            "claim_id": attrs["claim"],
            "subject_id": attrs["subject"], "attribute": attrs["attribute"],
            "value": attrs["value"],
            "evidence_ids": _split_xml_list(attrs["evidence"]), **fields,
        })
    for child in sections["relations"]:
        if child.tag != "relation" or list(child):
            raise ValueError("<relations> may contain only <relation> elements")
        attrs, fields = assertion_attrs(child, {"claim", "src", "dst", "type", "evidence"})
        body["relations"].append({
            "claim_id": attrs["claim"],
            "src_id": attrs["src"], "dst_id": attrs["dst"],
            "relation": attrs["type"],
            "evidence_ids": _split_xml_list(attrs["evidence"]), **fields,
        })
    for child in sections["events"]:
        if child.tag != "event":
            raise ValueError("<events> may contain only <event> elements")
        if list(child):
            # Explicit argument fields (item 2): entity= or literal=, never a separator
            # character carrying the distinction. The two encodings never mix.
            attrs, fields = assertion_attrs(child, {"claim", "action", "evidence"})
            arguments = []
            for arg in child:
                if arg.tag != "arg" or list(arg) or "role" not in arg.attrib:
                    raise ValueError("<event> children must be <arg role=...> elements")
                kind = _xml_attrs(arg, {"role", "entity"} if "entity" in arg.attrib else {"role", "literal"})
                arguments.append({"role": kind["role"], **(
                    {"entity_id": kind["entity"]} if "entity" in kind else {"value": kind["literal"]})})
        else:
            attrs, fields = assertion_attrs(child, {"claim", "action", "args", "evidence"})
            arguments = _parse_event_arguments(attrs["args"])
        body["events"].append({
            "claim_id": attrs["claim"], "action": attrs["action"],
            "arguments": arguments,
            "evidence_ids": _split_xml_list(attrs["evidence"]), **fields,
        })
    return body


def _parse_normalization_xml(text: str) -> dict[str, list[dict[str, Any]]]:
    """Isolate row-shape errors; invalid XML/envelopes still fail closed.

    Never repair duplicate XML attributes or truncated documents: there is no
    unambiguous interpretation of those bytes. Preserve failed rows for validation.
    """
    root = ET.fromstring(_xml_body(text))
    names = ("decisions", "entities", "facts", "relations", "events")
    tags = [child.tag for child in root]
    # <references> (step 4) and <accounting> (item 5) are optional so every earlier
    # variant's responses still parse.
    optional = [name for name in ("references", "accounting") if name in tags]
    if root.tag != "knowledge" or root.attrib or sorted(tags) != sorted([*names, *optional]):
        raise ValueError("normalization must contain exactly the five knowledge sections")
    body: dict[str, list[dict[str, Any]]] = {name: [] for name in (*names, "references", "accounting")}
    for section in root:
        if section.attrib or (section.text or "").strip():
            raise ValueError("normalization sections must contain only rows")
        for child in section:
            if (child.tail or "").strip():
                raise ValueError("unexpected text between normalization rows")
            if section.tag == "references":
                try:
                    if child.tag != "reference" or list(child):
                        raise ValueError("<references> may contain only <reference> elements")
                    attrs = _xml_attrs(child, {"claim", "id", "surface", "refers_to", "candidate"})
                    body["references"].append({
                        "claim_id": attrs["claim"], "local_id": attrs["id"],
                        "surface": attrs["surface"], "refers_to": attrs["refers_to"],
                        "candidate_id": attrs["candidate"].strip() or None,
                    })
                except ValueError as exc:
                    body["references"].append({"claim_id": child.get("claim"), "_parse_error": str(exc),
                                               "_raw_xml": ET.tostring(child, encoding="unicode")})
                continue
            if section.tag == "accounting":
                try:
                    if child.tag != "assertion" or list(child):
                        raise ValueError("<accounting> may contain only <assertion> elements")
                    attrs = _xml_attrs(child, {"claim", "id", "statement", "outcome", "reason"})
                    body["accounting"].append({
                        "claim_id": attrs["claim"], "assertion_id": attrs["id"],
                        "statement": attrs["statement"], "outcome": attrs["outcome"],
                        "reason": attrs["reason"],
                    })
                except ValueError as exc:
                    body["accounting"].append({"claim_id": child.get("claim"), "_parse_error": str(exc),
                                               "_raw_xml": ET.tostring(child, encoding="unicode")})
                continue
            envelope = ET.Element("knowledge")
            for name in names:
                target = ET.SubElement(envelope, name)
                if name == section.tag:
                    target.append(child)
            try:
                parsed = _parse_normalization_row_document(ET.tostring(envelope, encoding="unicode"))
                body[section.tag].extend(parsed[section.tag])
            except (ValueError, ET.ParseError) as exc:
                body[section.tag].append({
                    "claim_id": child.get("claim"),
                    "_parse_error": str(exc),
                    "_raw_xml": ET.tostring(child, encoding="unicode"),
                })
    return body


def validate_discovery(
    text: str, case: dict[str, Any], claim_ceiling: int = CLAIM_CEILINGS["baseline"],
    variant: str = "baseline",
) -> dict[str, Any]:
    claims, total_claim_tags, malformed = _parse_discovery_xml(text, variant)
    if total_claim_tags > claim_ceiling:
        raise ValueError(
            f"discovery returned {total_claim_tags} claims; this variant's ceiling is "
            f"{claim_ceiling}"
        )
    passage_map = {row["id"]: row for row in _source_passages(case["source"])}
    accepted: list[dict[str, Any]] = []
    # A malformed <claim> tag is dropped, not fatal -- it starts out rejected so the
    # other, well-formed claims in the same response still get a result.
    rejected: list[dict[str, Any]] = [
        {"index": item["index"], "row": {"tag": item["tag"], **item["attrib"]},
         "reason": item["reason"]}
        for item in malformed
    ]
    seen: set[str] = set()
    for index, item in claims:
        raw = item.model_dump()
        try:
            if item.claim_id in seen:
                raise ValueError("duplicate claim ID")
            seen.add(item.claim_id)
            evidence = _materialize_evidence(passage_map, item.evidence_ids)
            context = _materialize_evidence(passage_map, item.context_ids)
            raw["evidence"] = evidence
            raw["context"] = context
            accepted.append(raw)
        except ValueError as exc:
            rejected.append({"index": index, "row": raw, "reason": str(exc)})
    # Hitting the ceiling is not a parse failure, but is a bounded-comparison warning:
    # the chapter may be incomplete. The ceiling is a property of the model response,
    # not local validation: a malformed or rejected row still consumed one of the
    # claim slots.
    ceiling_reached = total_claim_tags == claim_ceiling
    return {
        "accepted": accepted,
        "rejected": rejected,
        "claim_ceiling": claim_ceiling,
        "claim_ceiling_reached": ceiling_reached,
        "potentially_incomplete": ceiling_reached,
        "malformed_claim_tags": len(malformed),
    }


def _top_level_lists(body: Any) -> dict[str, list[Any]]:
    if not isinstance(body, dict):
        raise ValueError("extraction response must be a JSON object")
    expected = ("decisions", "entities", "facts", "relations", "events")
    if set(body) - {"references", "accounting"} != set(expected):
        raise ValueError(f"extraction response keys must be exactly {expected}")
    if any(not isinstance(body[key], list) for key in expected):
        raise ValueError("every extraction response field must be an array")
    return {"references": [], "accounting": [], **body}


def _normalization_passage_ids(
    case: dict[str, Any], discovery: dict[str, Any] | None,
    supplied_passage_ids: set[str] | None,
) -> set[str]:
    if supplied_passage_ids is not None:
        return set(supplied_passage_ids)
    if discovery is not None:
        ids: set[str] = set()
        for claim in discovery.get("accepted", []):
            ids.update(claim.get("evidence_ids", []))
            ids.update(claim.get("context_ids", []))
        return ids
    # Direct callers with no request package are retained for unit-level validation.
    # The live run always supplies discovery, making the package boundary strict.
    return {row["id"] for row in _source_passages(case["source"])}


def validate_extraction(
    text: str,
    case: dict[str, Any],
    discovery: dict[str, Any] | None = None,
    supplied_passage_ids: set[str] | None = None,
    require_assertions: bool = False,
) -> dict[str, Any]:
    """Validate rows independently so one bad proposal remains visible but contained."""
    body = _top_level_lists(_parse_normalization_xml(text))
    source = case["source"]
    passage_map = {row["id"]: row for row in _source_passages(source)}
    package_ids = _normalization_passage_ids(case, discovery, supplied_passage_ids)
    claim_rows = (discovery or {}).get("accepted", [])
    claims = {
        row["claim_id"]: row for row in claim_rows
        if isinstance(row, dict) and isinstance(row.get("claim_id"), str)
    }

    def claim_passage_ids(claim_id: str) -> set[str]:
        claim = claims[claim_id]
        return (
            set(claim.get("evidence_ids", []))
            | set(claim.get("context_ids", []))
        ) & package_ids

    def participant_anchor(
        aliases: list[str], claim_id: str, evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return source witnesses, not a semantic verdict about subject/role.

        Automatically supplied neighbors may anchor a name only if discovery also
        names it. Thus a sword in the previous passage cannot acquire a spider's
        emblem simply because the context window contains both.
        """
        claim = claims[claim_id]
        automatic = set(claim.get("automatic_context_ids", []))
        witnesses = []
        ids = {row["id"] for row in evidence} | set(claim.get("context_ids", []))
        for pid in sorted(ids & claim_passage_ids(claim_id)):
            passage = passage_map[pid]
            if any(alias in passage["text"] and (
                pid not in automatic or alias in claim.get("claim_source", "")
            ) for alias in aliases):
                witnesses.append(dict(passage))
        return witnesses

    allowed_kinds = set(case["ontology"]["kinds"])
    # References are not graph records; they are reported as unresolved_references.
    accepted: dict[str, list[dict[str, Any]]] = {
        key: [] for key in body if key not in {"references", "accounting"}}
    rejected: list[dict[str, Any]] = []
    entities: dict[str, dict[str, Any]] = {}
    name_map: dict[str, str] = {}

    def reject(section: str, index: int, row: Any, reason: str) -> None:
        rejected.append({"section": section, "index": index, "row": row, "reason": reason})

    decisions: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(body["decisions"]):
        try:
            item = ClaimDecision.model_validate(raw)
            if item.claim_id not in claims:
                raise ValueError("decision references an unknown discovered claim")
            if item.claim_id in decisions:
                raise ValueError("duplicate claim decision")
            evidence = _materialize_evidence(
                passage_map, item.evidence_ids, claim_passage_ids(item.claim_id),
                "claim evidence/context",
            )
            if item.verdict != "insufficient_evidence" and not evidence:
                raise ValueError("supported or contradicted decision needs evidence")
            row = item.model_dump()
            row["evidence"] = evidence
            decisions[item.claim_id] = row
            accepted["decisions"].append(row)
        except (ValidationError, ValueError) as exc:
            reject("decisions", index, raw, str(exc))

    for claim_id in claims:
        if claim_id not in decisions:
            reject("decisions", -1, {"claim_id": claim_id},
                   "discovered claim has no normalization decision")
    missing_decisions = [claim_id for claim_id in claims if claim_id not in decisions]
    if missing_decisions:
        raise ValueError(
            "normalization must provide exactly one valid decision for every discovered "
            f"claim; missing {missing_decisions[0]}"
        )

    for index, raw in enumerate(body["entities"]):
        try:
            item = EntityProposal.model_validate(raw)
            if item.claim_id not in claims:
                raise ValueError("entity references an unknown discovered claim")
            if item.claim_id not in decisions:
                raise ValueError("entity references a claim without a decision")
            if item.claim_id in decisions and decisions[item.claim_id]["verdict"] != "supported":
                raise ValueError("graph record references a claim that was not supported")
            if item.kind not in allowed_kinds:
                raise ValueError("entity kind is outside the benchmark ontology")
            # The search space is the claim's own package; the evidence actually kept is
            # the subset that names the entity (the anchors below).
            package = [dict(passage_map[pid]) for pid in sorted(claim_passage_ids(item.claim_id))]
            evidence = package
            correction = None
            if (item.canonical_source not in source
                    and item.canonical_source in {"原文", "passage", "SOURCE_NAME", "SOURCE", "原名"}):
                # Repair only an unambiguous source-grounded alias, never an arbitrary
                # invalid name. This local formatting repair does not merge identities.
                grounded = list(dict.fromkeys(
                    alias for alias in item.source_aliases
                    if participant_anchor([alias], item.claim_id, evidence)
                ))
                subject = claims[item.claim_id].get("memory", {}).get("subject")
                preferred = [alias for alias in grounded if alias == subject]
                choices = preferred or grounded
                if len(choices) != 1:
                    raise ValueError("placeholder canonical source has no unambiguous grounded alias")
                correction = {"field": "canonical_source", "original": item.canonical_source,
                              "replacement": choices[0], "reason": "unambiguous grounded alias"}
                item = item.model_copy(update={"canonical_source": choices[0]})
            aliases = list(dict.fromkeys([item.canonical_source, *item.source_aliases]))
            if any(alias not in source for alias in aliases):
                raise ValueError("source name or alias is absent from the chapter")
            anchors = participant_anchor(aliases, item.claim_id, evidence)
            if not anchors:
                raise ValueError("no passage in the claim's package names the entity")
            if item.local_id in entities:
                # The prompt says "reuse IDs across claims"; a model that re-declares the
                # same entity under each claim is obeying it. An identical redeclaration
                # adds that claim's anchors to the one entity and creates nothing new; any
                # differing field is a conflicting identity and still fails closed.
                prior = entities[item.local_id]
                if ((prior["canonical_source"], prior["english_name"], prior["kind"],
                     set(prior["source_aliases"]))
                        != (item.canonical_source, item.english_name, item.kind, set(aliases))):
                    raise ValueError("conflicting redeclaration of local entity ID")
                known = {passage["id"] for passage in prior["evidence"]}
                prior["evidence"].extend(p for p in anchors if p["id"] not in known)
                prior["evidence_ids"] = [passage["id"] for passage in prior["evidence"]]
                prior["binding_evidence"] = prior["evidence"]
                prior["declared_by_claims"].append(item.claim_id)
                continue
            conflicts =[alias for alias in aliases if alias in name_map and name_map[alias] != item.english_name]
            if conflicts:
                raise ValueError(f"conflicting English mapping for {conflicts[0]!r}")
            row = item.model_dump()
            row["evidence_ids"] = [passage["id"] for passage in anchors]
            row["evidence"] = anchors
            row["evidence_source"] = "derived"
            row["source_aliases"] = aliases
            row["binding_evidence"] = anchors
            row["declared_by_claims"] = [item.claim_id]
            if correction:
                row["format_corrections"] = [correction]
            entities[item.local_id] = row
            for alias in aliases:
                name_map[alias] = item.english_name
            accepted["entities"].append(row)
        except (ValidationError, ValueError) as exc:
            reject("entities", index, raw, str(exc))

    # Step 4: unresolved references. Validated like an entity's grounding (the exact
    # surface must appear in the claim's own package) but never named or merged --
    # RESOLVE decides identity later from chapter-gated state (§0, §12 risk #2).
    references: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(body.get("references", [])):
        try:
            item = ReferenceProposal.model_validate(raw)
            if item.claim_id not in claims:
                raise ValueError("reference names an unknown discovered claim")
            if (decisions.get(item.claim_id) or {}).get("verdict") != "supported":
                raise ValueError("graph record references a claim that was not supported")
            if item.refers_to not in allowed_kinds | {"unknown"}:
                raise ValueError("reference kind is outside the benchmark ontology")
            if item.candidate_id is not None and item.candidate_id not in entities:
                raise ValueError("reference candidate is a rejected or unknown entity")
            package = [dict(passage_map[pid]) for pid in sorted(claim_passage_ids(item.claim_id))]
            anchors = participant_anchor([item.surface], item.claim_id, package)
            if not anchors:
                raise ValueError("no passage in the claim's package contains the reference surface")
            if item.local_id in references:
                prior = references[item.local_id]
                if (prior["surface"], prior["refers_to"], prior["candidate_id"]) != (
                        item.surface, item.refers_to, item.candidate_id):
                    raise ValueError("conflicting redeclaration of local reference ID")
                prior["declared_by_claims"].append(item.claim_id)
                continue
            references[item.local_id] = {
                **item.model_dump(), "source_aliases": [item.surface],
                "evidence_ids": [p["id"] for p in anchors], "evidence": anchors,
                "declared_by_claims": [item.claim_id], "resolution_status": "unresolved",
            }
        except (ValidationError, ValueError) as exc:
            reject("references", index, raw, str(exc))
    participants = {**entities, **references}

    # Item 5: per-assertion accounting. Validated before records so each record can be
    # tied to the assertion it represents.
    accounts: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(body.get("accounting", [])):
        try:
            item = AssertionAccount.model_validate(raw)
            if item.claim_id not in claims:
                raise ValueError("assertion names an unknown discovered claim")
            if (decisions.get(item.claim_id) or {}).get("verdict") != "supported":
                raise ValueError("assertion accounts for a claim that was not supported")
            if not item.assertion_id.startswith(f"{item.claim_id}.a"):
                raise ValueError("assertion ID does not belong to its claim")
            if item.assertion_id in accounts:
                raise ValueError("duplicate assertion ID")
            if item.outcome != "represented" and not item.reason:
                raise ValueError("an unresolved or omitted assertion needs a reason")
            accounts[item.assertion_id] = {**item.model_dump(), "accepted_records": [], "rejected_records": []}
        except (ValidationError, ValueError) as exc:
            reject("accounting", index, raw, str(exc))

    validators = {
        "facts": FactProposal,
        "relations": RelationProposal,
        "events": EventProposal,
    }
    for section, model_type in validators.items():
        seen: set[str] = set()
        for index, raw in enumerate(body[section]):
            try:
                # Drop citations outside the claim's package instead of rejecting the
                # whole record, as long as at least one in-package citation remains. The
                # dropped IDs are kept on the row so the repair stays auditable.
                dropped_citations: list[str] = []
                if isinstance(raw, dict) and raw.get("claim_id") in claims:
                    allowed = claim_passage_ids(raw["claim_id"])
                    cited = list(dict.fromkeys(raw.get("evidence_ids", [])))
                    kept = [pid for pid in cited if pid in allowed]
                    if kept and len(kept) < len(cited):
                        dropped_citations = [pid for pid in cited if pid not in allowed]
                        raw = {**raw, "evidence_ids": kept}
                item = model_type.model_validate(raw)
                if item.claim_id not in claims:
                    raise ValueError(f"{section} references an unknown discovered claim")
                if item.claim_id in decisions and decisions[item.claim_id]["verdict"] != "supported":
                    raise ValueError(f"{section} references a claim that was not supported")
                if item.claim_id not in decisions:
                    raise ValueError(f"{section} references a claim without a decision")
                evidence = _materialize_evidence(
                    passage_map, item.evidence_ids, claim_passage_ids(item.claim_id),
                    "claim evidence/context",
                )
                if section == "facts" and item.subject_id not in participants:
                    raise ValueError("fact references a rejected or unknown entity")
                if section == "relations" and (
                    item.src_id not in participants or item.dst_id not in participants
                ):
                    raise ValueError("relation references a rejected or unknown entity")
                if section == "events" and any(
                    argument.entity_id is not None and argument.entity_id not in participants
                    for argument in item.arguments
                ):
                    raise ValueError("event references a rejected or unknown entity")
                if require_assertions:
                    missing = [name for name in ("assertion_id", "source_span", "polarity", "attribution",
                                                 "condition", "temporal") if getattr(item, name) is None]
                    if missing:
                        raise ValueError(f"record lacks explicit assertion fields: {missing}")
                    if item.assertion_id not in accounts or accounts[item.assertion_id]["claim_id"] != item.claim_id:
                        raise ValueError("record names an assertion its claim did not account for")
                    # Provenance only: the span must be source words; whether the normalized
                    # value means the same thing is the verifier's semantic question.
                    if not any(item.source_span in passage["text"] for passage in evidence):
                        raise ValueError("source span is absent from record evidence")
                    if item.attribution != "narrator" and item.attribution not in participants:
                        raise ValueError("attribution is neither narrator nor a known participant")
                if section == "events":
                    # A literal is source-grounded like a name: copied, not paraphrased.
                    for argument in item.arguments:
                        if argument.value is not None and not any(
                                argument.value in passage["text"] for passage in evidence):
                            raise ValueError(
                                f"literal value is absent from record evidence: {argument.role}")
                # §0: fail closed on unanchored participant IDs. This is a necessary
                # grounding check, not a semantic proof of the participant's role.
                refs = ([item.subject_id] if section == "facts" else
                        [item.src_id, item.dst_id] if section == "relations" else
                        [argument.entity_id for argument in item.arguments
                         if argument.entity_id is not None])
                bindings = {}
                for entity_id in refs:
                    aliases = participants[entity_id]["source_aliases"]
                    anchors = participant_anchor(aliases, item.claim_id, evidence)
                    if not anchors:
                        raise ValueError(
                            f"unanchored participant {entity_id}: its source names are absent "
                            "from record evidence and claim context; role requires review"
                        )
                    bindings[entity_id] = anchors
                # exclude_none: an argument carries entity_id or value, never a null other.
                row = item.model_dump(exclude_none=True)
                duplicate_key = _digest(row)
                if duplicate_key in seen:
                    raise ValueError("duplicate proposal")
                seen.add(duplicate_key)
                row["evidence"] = evidence
                row["binding_evidence"] = bindings
                if dropped_citations:
                    row["citation_corrections"] = {
                        "dropped": dropped_citations,
                        "reason": "outside the claim's evidence/context package",
                    }
                # Context can establish a candidate identity, never prove that the
                # model assigned the correct semantic role (e.g. rider vs mount).
                row["binding_review_required"] = any(
                    not any(alias in passage["text"] for alias in participants[entity_id]["source_aliases"]
                            for passage in evidence)
                    for entity_id in refs
                )
                accepted[section].append(row)
            except (ValidationError, ValueError) as exc:
                reject(section, index, raw, str(exc))

    for section in ("facts", "relations", "events"):
        for row in accepted[section]:
            if row.get("assertion_id") in accounts:
                accounts[row["assertion_id"]]["accepted_records"].append(section)
    for rejection in rejected:
        row = rejection.get("row")
        if isinstance(row, dict) and row.get("assertion_id") in accounts:
            accounts[row["assertion_id"]]["rejected_records"].append(rejection["reason"])
    for account in accounts.values():
        # "Represented" is the model's claim; "lost" is what validation made of it.
        account["status"] = (account["outcome"] if account["outcome"] != "represented"
                             else "represented" if account["accepted_records"] else "lost")
    supported_ids = [cid for cid, d in decisions.items() if d["verdict"] == "supported"]
    assertion_accounting = {
        "required": require_assertions,
        "assertions": list(accounts.values()),
        "counts": {status: sum(a["status"] == status for a in accounts.values())
                   for status in ("represented", "lost", "unresolved", "omitted")},
        "supported_claims_without_assertions": [
            cid for cid in supported_ids if not any(a["claim_id"] == cid for a in accounts.values())],
    }

    by_claim: dict[str, dict[str, Any]] = {
        claim_id: {
            "claim_id": claim_id,
            # Keep the discovered proposition with its decision so a report can trace
            # a loss without joining against a second artifact section.
            "claim": {
                key: claim.get(key)
                for key in ("claim_id", "claim_source", "evidence_ids", "context_ids")
                if key in claim
            },
            "decision": decisions.get(claim_id),
            "accepted_records": [],
            "validation_rejections": [],
            "schema_limitations": [],
        }
        for claim_id, claim in claims.items()
    }
    unlinked_rejections: list[dict[str, Any]] = []
    for section, rows in accepted.items():
        if section == "decisions":
            continue
        for row in rows:
            claim_id = row["claim_id"]
            if claim_id not in by_claim:
                continue
            by_claim[claim_id]["accepted_records"].append({
                "section": section,
                # Keep this compact and stable; the full accepted row remains above.
                "record": row,
            })
    for rejection in rejected:
        row = rejection.get("row")
        claim_id = row.get("claim_id") if isinstance(row, dict) else None
        if isinstance(claim_id, str) and claim_id in by_claim:
            diagnostic = {
                "section": rejection["section"], "index": rejection["index"],
                "reason": rejection["reason"], "row": row,
            }
            by_claim[claim_id]["validation_rejections"].append(diagnostic)
            # These are representation failures, rather than evidence failures. Keep
            # them explicit so a supported claim is not mistaken for a bad claim.
            reason = str(rejection.get("reason", "")).casefold()
            if any(marker in reason for marker in (
                "unknown entity", "rejected or unknown entity",
                "entity kind is outside", "cannot be represented", "schema",
            )):
                by_claim[claim_id]["schema_limitations"].append(diagnostic)
        elif isinstance(claim_id, str):
            unlinked_rejections.append(rejection)
    supported_but_unrepresented = [
        claim_id for claim_id, item in by_claim.items()
        if (item.get("decision") or {}).get("verdict") == "supported"
        and not item["accepted_records"]
    ]
    for claim_id in supported_but_unrepresented:
        item = by_claim[claim_id]
        if not item["schema_limitations"]:
            item["schema_limitations"].append({
                "claim_id": claim_id,
                "section": "representation",
                "index": -1,
                "reason": (
                    "supported claim has no accepted graph record under the closed "
                    "benchmark schema; no entity or free-form record was invented"
                ),
            })
    return {
        "accepted": accepted, "rejected": rejected,
        "name_map": dict(sorted(name_map.items())),
        "unresolved_references": list(references.values()),
        "assertion_accounting": assertion_accounting,
        "claim_diagnostics": list(by_claim.values()),
        "unlinked_rejections": unlinked_rejections,
        "supported_but_unrepresented": supported_but_unrepresented,
        "supported_claims_without_records": supported_but_unrepresented,
        "schema_limitations": [
            {"claim_id": claim_id, "records": item["schema_limitations"]}
            for claim_id, item in by_claim.items()
            if item["schema_limitations"]
        ],
    }


def revalidate_artifact(artifact: dict[str, Any], dataset_path: str | Path) -> dict[str, Any]:
    """Re-run the current normalization validator over a saved response; no model call.

    Validator fixes change how much of a saved response lands, never what the model
    said. Replaying the stored bytes isolates representation losses from model quality
    at zero token cost. The replay is labelled so it is never mistaken for a new run.
    """
    case = load_case(dataset_path, artifact["chapter"])
    if isinstance(artifact.get("ontology"), dict):
        # Replay against the ontology the model was shown, not today's.
        case = {**case, "ontology": artifact["ontology"]}
    if artifact.get("source_hash") not in {None, _digest(case["source"])}:
        raise ValueError("artifact source hash does not match the dataset chapter")
    discovery = artifact.get("discovery")
    response = next((row.get("response") for row in reversed(artifact.get("attempts", []))
                     if row.get("stage") == "normalize" and isinstance(row.get("response"), str)), None)
    if response is None and artifact.get("normalization_skipped") == "no_selected_claims":
        response = "<knowledge><decisions/><entities/><facts/><relations/><events/></knowledge>"
    if response is None:
        response = next((row.get("response") for row in reversed(artifact.get("cumulative_attempts", []))
                         if row.get("stage") == "normalize" and isinstance(row.get("response"), str)), None)
    if not isinstance(discovery, dict) or response is None:
        raise ValueError("artifact has no saved discovery and normalize response to replay")
    variant = (artifact.get("variants") or {}).get("normalization", "baseline")
    replay = {**artifact, "revalidation": {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "original_status": artifact.get("status"),
        "original_error": artifact.get("error"),
        "note": "saved response replayed through the current validator; no model call",
    }}
    replay.pop("error", None)
    try:
        selection_variant = (artifact.get("variants") or {}).get("selection", "none")
        if selection_variant not in {"none", "compact"}:
            raise ValueError(f"unknown selection variant: {selection_variant}")
        if selection_variant == "compact":
            discovery = selected_discovery(discovery, artifact.get("selection") or {}, _digest(case["source"]))
            replay["selected_discovery"] = discovery
        if artifact.get("normalization_skipped") and discovery["accepted"]:
            raise ValueError("normalization cannot be skipped for nonempty selected discovery")
        normalization_discovery = normalization_input(case, discovery, variant)
        replay["normalization_input"] = normalization_discovery
        replay["extraction"] = validate_extraction(response, case, normalization_discovery,
                                                   require_assertions=variant == "assertion")
        replay["status"] = "completed"
        if variant == "memory":
            replay["memory_candidates"] = memory_candidates(case, normalization_discovery, replay["extraction"])
    except (ValueError, ET.ParseError) as exc:
        replay["status"] = "failed"
        replay["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return replay


def validate_translation(text: str, source: str, name_map: dict[str, str]) -> dict[str, Any]:
    root = ET.fromstring(_xml_body(text))
    if root.tag != "translation" or root.attrib:
        raise ValueError("translation XML root must be <translation> without attributes")
    target_rows: list[tuple[str, str]] = []
    for child in root:
        if child.tag != "p" or list(child):
            raise ValueError("<translation> may contain only text-only <p> elements")
        attrs = _xml_attrs(child, {"id"})
        translated = (child.text or "").strip()
        if not translated:
            raise ValueError(f"translation passage is empty: {attrs['id']}")
        target_rows.append((attrs["id"], translated))
    source_passages = _source_passages(source)
    source_ids = [row["id"] for row in source_passages]
    target_ids = [row[0] for row in target_rows]
    if target_ids != source_ids:
        raise ValueError(
            f"translation passage IDs differ from source ({len(target_ids)} != {len(source_ids)})"
        )
    translation = "\n".join(row[1] for row in target_rows)
    required_names = sorted(set(name_map.values()))
    # Capitalization naturally changes at sentence boundaries and for common-noun
    # mappings (for example, ``Giant Lizard`` -> ``giant lizard``). Treat those as
    # the same rendering while still requiring every mapped English surface.
    translated_casefold = translation.casefold()
    missing = [name for name in required_names if name.casefold() not in translated_casefold]
    return {
        "translation": translation,
        "source_paragraphs": len(source_passages),
        "translation_paragraphs": len(target_rows),
        "paragraphs_preserved": target_ids == source_ids,
        "required_english_names": required_names,
        "missing_english_names": missing,
    }


def _request_identity(request: dict[str, Any], provider_name: str) -> str:
    # ``variant=baseline`` was added after the original artifacts were written. It
    # does not change the baseline prompt, so keep those artifacts reusable by leaving
    # that marker out of the legacy identity. Revised variants retain their marker so
    # their cached results cannot be mixed accidentally.
    identity_request = dict(request)
    if identity_request.get("variant") == "baseline":
        identity_request.pop("variant")
    return _digest({"provider": provider_name, **identity_request})


def prompt_hashes(attempts: list[dict[str, Any]]) -> dict[str, str]:
    """Return stable hashes of the exact prompts used by each stage.

    The request itself remains in each attempt for replay; this compact index is for
    persistence provenance and makes it possible to compare a production run with
    the pinned experiment without storing prompt text in every database row.
    """
    hashes: dict[str, str] = {}
    for attempt in attempts:
        request = attempt.get("request")
        stage = attempt.get("stage")
        if isinstance(stage, str) and isinstance(request, dict):
            hashes[stage] = _digest({
                "system": request.get("system", ""),
                "prompt": request.get("prompt", ""),
            })
    return hashes


def _count_request(provider: Any, request: dict[str, Any]) -> tuple[int, str]:
    counter = getattr(provider, "count_request_tokens", None)
    if callable(counter):
        try:
            return int(counter(
                request["prompt"], system=request["system"], model=request["model"],
                json_schema=request["json_schema"], json_mode=request["json_mode"],
            )), "provider_token_counter"
        except Exception:
            pass
    material = request["system"] + request["prompt"] + json.dumps(request["json_schema"])
    return len(material.encode()), "utf8_bytes_estimate"


def source_baseline(provider: Any, source: str, model: str) -> dict[str, Any]:
    counter = getattr(provider, "count_request_tokens", None)
    if callable(counter):
        try:
            full = int(counter(source, model=model))
            empty = int(counter("", model=model))
            return {
                "content_tokens": max(1, full - empty),
                "minimal_request_tokens": full,
                "empty_request_tokens": empty,
                "method": "provider_token_counter_difference",
                # This is a local tokenizer calculation, not usage returned by an
                # actual provider response. It is useful for planning only.
                "estimated": True,
            }
        except Exception:
            pass
    size = max(1, len(source.encode()))
    return {
        "content_tokens": size,
        "minimal_request_tokens": size,
        "empty_request_tokens": 0,
        "method": "utf8_bytes_estimate",
        "estimated": True,
    }


def _reuse_attempt(
    artifact: dict[str, Any] | None, stage: str, identity: str
) -> dict[str, Any] | None:
    if not artifact:
        return None
    for attempt in reversed([*artifact.get("cumulative_attempts", []), *artifact.get("attempts", [])]):
        if (
            attempt.get("stage") == stage
            and attempt.get("request_digest") == identity
            and attempt.get("status") == "completed"
            and attempt.get("validation_status") == "accepted"
            and isinstance(attempt.get("response"), str)
        ):
            return attempt
    return None


def _reuse_metadata_matches(
    artifact: dict[str, Any] | None, source_hash: str,
    discovery_variant: str, normalization_variant: str, stage: str,
    selection_variant: str = "none",
) -> bool:
    """Prevent a stage-only replay from silently mixing comparison variants."""
    if not artifact:
        return True
    artifact_source = artifact.get("source_hash")
    if artifact_source is not None and artifact_source != source_hash:
        return False
    variants = artifact.get("variants")
    if not isinstance(variants, dict):
        return True  # pre-variant artifacts remain readable
    if variants.get("discovery", "baseline") != discovery_variant:
        return False
    # Discovery is an independent reusable stage.  A discovered artifact may feed
    # either normalization policy for bounded comparison; translation depends on both.
    if stage in {"discover", "select", "normalize"}:
        return True
    return (variants.get("normalization", "baseline") == normalization_variant
            and variants.get("selection", "none") == selection_variant)


async def _execute_stage(
    provider: Any,
    provider_name: str,
    request: dict[str, Any],
    attempts: list[dict[str, Any]],
    reuse_artifact: dict[str, Any] | None,
) -> str:
    identity = _request_identity(request, provider_name)
    estimated_input, estimate_method = _count_request(provider, request)
    attempt: dict[str, Any] = {
        "attempt": len(attempts) + 1,
        "stage": request["stage"],
        "request_digest": identity,
        "transport": "api",
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "request": request,
        "estimated_input": estimated_input,
        "estimate_method": estimate_method,
        "source_copies": (
            request["prompt"].count('"source":')
            + request["prompt"].count("SOURCE PASSAGES:")
        ),
    }
    attempts.append(attempt)
    reused = _reuse_attempt(reuse_artifact, request["stage"], identity)
    if reused is not None:
        attempt.update({
            "transport": "reuse", "status": "completed", "response": reused["response"],
            "elapsed_seconds": 0.0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                      "cache_write_tokens": 0},
            "reused_attempt": reused.get("attempt"),
            "reused_request_digest": reused.get("request_digest"),
        })
        return reused["response"]
    started = time.monotonic()
    try:
        call_kwargs = {
            "system": request["system"], "json_mode": request["json_mode"],
            "cls": Class.BATCH, "model": request["model"],
            "json_schema": request["json_schema"],
            "max_output_tokens": request["max_output_tokens"],
        }
        if provider_name in {"groq", "openrouter"}:
            call_kwargs["reasoning_effort"] = request["reasoning_effort"]
        completion: Completion = await provider.complete(request["prompt"], **call_kwargs)
    except Exception as exc:
        details = {
            key: getattr(exc, key) for key in (
                "category", "retry_after_s", "exact_hint", "rate_limits", "rate_limit_details"
            ) if hasattr(exc, key)
        }
        partial = getattr(exc, "partial_completion", None)
        partial_fields: dict[str, Any] = {"usage": None}
        if isinstance(partial, Completion):
            partial_fields = {
                "response": partial.text,
                "served_provider": partial.served_provider,
                "served_model": partial.served_model,
                "usage": {
                    "input_tokens": partial.input_tokens,
                    "output_tokens": partial.output_tokens,
                    "cache_read_tokens": partial.cache_read_tokens,
                    "cache_write_tokens": partial.cache_write_tokens,
                },
                "rate_limits": partial.rate_limits,
            }
        attempt.update({
            "status": "failed", "elapsed_seconds": round(time.monotonic() - started, 6),
            "error": {"type": type(exc).__name__, "message": str(exc), **details},
            **partial_fields,
        })
        raise
    attempt.update({
        "status": "completed", "elapsed_seconds": round(time.monotonic() - started, 6),
        "response": completion.text,
        "served_provider": completion.served_provider,
        "served_model": completion.served_model,
        "usage": {
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "cache_read_tokens": completion.cache_read_tokens,
            "cache_write_tokens": completion.cache_write_tokens,
        },
        "rate_limits": completion.rate_limits,
    })
    return completion.text


async def execute_with_backoff(provider: Any, provider_name: str, request: dict[str, Any],
                               attempts: list[dict[str, Any]], reuse: dict[str, Any] | None,
                               tries: int = 5) -> str:
    """Honor a provider's exact retry-after on rate limits; every try stays in attempts.

    Groq's on-demand tier allows 8K tokens/minute, so a multi-call run is paced by the
    provider's own hint rather than by a guessed sleep. A daily quota (quota_exhausted)
    is not retried: waiting it out is the operator's call, not a benchmark's.
    """
    for number in range(tries):
        try:
            return await _execute_stage(provider, provider_name, request, attempts, reuse)
        except Exception as exc:
            wait = getattr(exc, "retry_after_s", None)
            # A transient server error with an explicit hint (OpenRouter's free tier sends
            # model_server_error + retry-after) is retried the same way.
            if (getattr(exc, "category", None) not in {"rate_limited", "model_server_error"}
                    or wait is None or number == tries - 1):
                raise
            # Honor the provider's retry hint, while ensuring retries back off even
            # when a provider repeatedly returns the same small hint.  There are at
            # most five attempts; operators can interrupt a run rather than having
            # the core silently discard a provider-directed delay.
            await asyncio.sleep(max(float(wait), float(2 ** number)))
    raise AssertionError("unreachable")


def call_metrics(
    attempts: list[dict[str, Any]], baseline: dict[str, Any], cached_input_discount: float
) -> dict[str, Any]:
    api = [row for row in attempts if row.get("transport") == "api"]
    completed = [row for row in api if row.get("status") == "completed"]
    usages = [row["usage"] for row in api if isinstance(row.get("usage"), dict)]
    input_tokens = sum(row.get("input_tokens", 0) for row in usages)
    output_tokens = sum(row.get("output_tokens", 0) for row in usages)
    cache_read = sum(row.get("cache_read_tokens", 0) for row in usages)
    denominator = baseline["content_tokens"]
    estimated_request_total = sum(row.get("estimated_input", 0) for row in api)
    unknown_usage = sum(row.get("usage") is None for row in api)
    reported_totals_complete = unknown_usage == 0
    return {
        "logical_api_calls": len(api),
        "successful_api_calls": len(completed),
        "failed_api_calls": sum(row.get("status") == "failed" for row in api),
        "reused_calls": sum(row.get("transport") == "reuse" for row in attempts),
        "provider_reported_input_tokens": input_tokens,
        "provider_reported_output_tokens": output_tokens,
        "provider_reported_cache_read_tokens": cache_read,
        "cache_adjusted_input_equivalent": (
            input_tokens - cache_read * cached_input_discount if reported_totals_complete else None
        ),
        "cached_input_discount_assumption": cached_input_discount,
        "estimated_request_total": estimated_request_total,
        "estimated_request_unit": (
            "tokens" if all(row.get("estimate_method") == "provider_token_counter" for row in api)
            else "mixed_or_utf8_bytes"
        ),
        "gross_context_multiplier": (
            round(input_tokens / denominator, 4)
            if reported_totals_complete and input_tokens and not baseline["estimated"]
            else 0.0 if not api else None
        ),
        "context_increase_percent": (
            round((input_tokens - denominator) * 100 / denominator, 2)
            if reported_totals_complete and input_tokens and not baseline["estimated"]
            else 0.0 if not api else None
        ),
        "estimated_context_multiplier": round(estimated_request_total / denominator, 4)
        if estimated_request_total and denominator else None,
        "provider_input_vs_estimated_baseline_multiplier": (
            round(input_tokens / denominator, 4)
            if reported_totals_complete and input_tokens and baseline["estimated"] else None
        ),
        "source_copies_sent": sum(row.get("source_copies", 0) for row in api),
        "max_provider_reported_request_tokens": max(
            (row["usage"].get("input_tokens", 0) for row in api
             if isinstance(row.get("usage"), dict)), default=0
        ),
        "unknown_usage_attempts": unknown_usage,
        "wall_seconds": round(sum(row.get("elapsed_seconds", 0.0) for row in api), 6),
    }


async def run_experiment(
    provider: Any,
    *,
    provider_name: str,
    model: str,
    translation_model: str | None = None,
    dataset_path: str | Path,
    chapter: int = 1,
    stage: Literal["all", "extract", "discover", "select", "normalize", "translate"] = "all",
    reuse_artifact: dict[str, Any] | None = None,
    discovery_max_output_tokens: int = 4096,
    selection_max_output_tokens: int = 4096,
    normalize_max_output_tokens: int = 4096,
    translate_max_output_tokens: int = 4096,
    cached_input_discount: float = 0.0,
    reasoning_effort: str = "low",
    discovery_variant: Literal["baseline", "revised", "support_first", "memory", "atomic"] = "baseline",
    normalization_variant: Literal["baseline", "revised", "support_first", "memory", "representation", "assertion"] = "baseline",
    selection_variant: Literal["none", "compact"] = "none",
) -> dict[str, Any]:
    case = load_case(dataset_path, chapter)
    translation_model = translation_model or model
    baseline = source_baseline(provider, case["source"], model)
    source_hash = _digest(case["source"])
    reuse_compatible = _reuse_metadata_matches(
        reuse_artifact, source_hash, discovery_variant, normalization_variant, stage, selection_variant
    )
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "format": "fact-first-api-v3" if selection_variant != "none" else "fact-first-api-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_name,
        "requested_model": model,
        "requested_translation_model": translation_model,
        "dataset": str(dataset_path),
        "chapter": chapter,
        "split": case["split"],
        "source_hash": source_hash,
        "variants": {
            "discovery": discovery_variant,
            "normalization": normalization_variant,
            "selection": selection_variant,
        },
        "artifact_label": (
            f"discover={discovery_variant};normalize={normalization_variant};"
            f"select={selection_variant};"
            f"source={_digest(case['source'])}"
        ),
        "source_characters": len(case["source"]),
        "ontology": case["ontology"],
        "source_baseline": baseline,
        "attempts": attempts,
        "status": "running",
    }
    try:
        if normalization_variant == "memory" and discovery_variant != "memory":
            raise ValueError("memory normalization requires memory discovery with comparison metadata")
        if selection_variant not in {"none", "compact"}:
            raise ValueError(f"unknown selection variant: {selection_variant}")
        if stage == "select" and selection_variant == "none":
            raise ValueError("select-only mode requires --selection-variant compact")
        if reuse_artifact and not reuse_compatible and stage in {"select", "normalize", "translate"}:
            raise ValueError(
                "reuse artifact source/variant label does not match requested run; "
                "use an artifact from the same bounded comparison variant"
            )
        discovery: dict[str, Any] | None = None
        extraction: dict[str, Any] | None = None

        if stage in {"all", "extract", "discover"}:
            request = discovery_request(
                case, model, discovery_max_output_tokens, reasoning_effort, discovery_variant
            )
            raw = await execute_with_backoff(provider, provider_name, request, attempts, reuse_artifact)
            discovery = validate_discovery(raw, case, CLAIM_CEILINGS[discovery_variant], discovery_variant)
            attempts[-1]["validation_status"] = "accepted"
            result["discovery"] = discovery

        if stage in {"select", "normalize"}:
            discovery = (reuse_artifact or {}).get("discovery")
            if not isinstance(discovery, dict) or not isinstance(discovery.get("accepted"), list):
                raise ValueError("select/normalize-only mode needs a reusable accepted discovery")
            result["discovery"] = discovery

        # §0.7: reuse source-bound discovery; selection has its own prompt/cache key.
        # The original discovery remains the audit trail even when every claim is omitted.
        if stage in {"all", "extract", "select", "normalize"} and selection_variant == "compact":
            assert discovery is not None
            if discovery["accepted"]:
                request = selection_request(case, discovery, _source_passages(case["source"]),
                                            model, selection_max_output_tokens, reasoning_effort)
                raw = await execute_with_backoff(provider, provider_name, request, attempts, reuse_artifact)
                result["selection"] = validate_selection(_xml_body(raw), discovery, source_hash)
                attempts[-1]["validation_status"] = "accepted"
            else:
                result["selection"] = validate_selection("<selection/>", discovery, source_hash)
            discovery = selected_discovery(discovery, result["selection"], source_hash)
            result["selected_discovery"] = discovery

        if stage in {"all", "extract", "normalize"}:
            assert discovery is not None
            if discovery_variant == "memory":
                for claim in discovery["accepted"]:
                    MemoryClaimProposal.model_validate({
                        key: value for key, value in claim.items()
                        if key not in {"evidence", "context", "automatic_context_ids"}
                    })
            normalization_discovery = normalization_input(case, discovery, normalization_variant)
            result["normalization_input"] = normalization_discovery
            request = normalization_request(
                case, discovery, model, normalize_max_output_tokens, reasoning_effort,
                normalization_variant,
            )
            if discovery["accepted"]:
                raw = await execute_with_backoff(provider, provider_name, request, attempts, reuse_artifact)
            else:
                # Empty memory is a valid result, not a reason to generate filler.
                raw = "<knowledge><decisions/><entities/><facts/><relations/><events/></knowledge>"
                result["normalization_skipped"] = "no_selected_claims"
            extraction = validate_extraction(raw, case, normalization_discovery,
                                             require_assertions=normalization_variant == "assertion")
            if discovery["accepted"]:
                attempts[-1]["validation_status"] = "accepted"
            result["extraction"] = extraction
            if discovery_variant == "memory":
                result["memory_candidates"] = memory_candidates(case, normalization_discovery, extraction)

        if stage == "translate":
            extraction = (reuse_artifact or {}).get("extraction")
            if not isinstance(extraction, dict) or not isinstance(extraction.get("name_map"), dict):
                raise ValueError("translation-only mode needs a reusable accepted normalization")
            result["extraction"] = extraction
            for key in ("discovery", "selection", "selected_discovery", "normalization_input", "normalization_skipped"):
                if key in (reuse_artifact or {}):
                    result[key] = reuse_artifact[key]

        if stage in {"all", "translate"}:
            assert extraction is not None
            request = translation_request(
                case, extraction["name_map"], translation_model, translate_max_output_tokens,
                reasoning_effort,
            )
            raw = await execute_with_backoff(provider, provider_name, request, attempts, reuse_artifact)
            result["translation"] = validate_translation(
                raw, case["source"], extraction["name_map"]
            )
            attempts[-1]["validation_status"] = "accepted"
        result["status"] = "completed"
    except Exception as exc:
        if attempts and attempts[-1].get("status") == "completed" and "validation_status" not in attempts[-1]:
            attempts[-1]["validation_status"] = "rejected"
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        result["run_metrics"] = call_metrics(attempts, baseline, cached_input_discount)
        prior_attempts = (reuse_artifact or {}).get("cumulative_attempts")
        if not isinstance(prior_attempts, list):
            prior_attempts = list((reuse_artifact or {}).get("attempts", []))
        cumulative = list(prior_attempts) + attempts
        result["cumulative_attempts"] = cumulative
        result["cumulative_metrics"] = call_metrics(cumulative, baseline, cached_input_discount)
        effective: list[dict[str, Any]] = []
        for wanted in ("discover", "select", "normalize", "translate"):
            if wanted == "select" and selection_variant == "none":
                continue
            if wanted == "normalize" and result.get("normalization_skipped"):
                continue
            if wanted == "normalize" and "extraction" not in result:
                continue
            if wanted == "translate" and "translation" not in result:
                continue
            if wanted == "select" and "selection" not in result:
                continue
            current = next((row for row in reversed(attempts)
                            if row.get("stage") == wanted
                            and row.get("validation_status") == "accepted"), None)
            if current and current.get("transport") == "api":
                effective.append(current)
                continue
            origin = next((row for row in reversed(prior_attempts)
                           if row.get("stage") == wanted
                           and row.get("status") == "completed"
                           and row.get("validation_status") == "accepted"), None)
            if origin:
                effective.append(origin)
        result["effective_pipeline_metrics"] = call_metrics(
            effective, baseline, cached_input_discount
        )
        # §0 provenance: keep the exact experimental baseline and served model details
        # alongside the immutable stage artifact.  Persistence adapters can copy this
        # object verbatim into run provenance without reconstructing it from logs.
        served = [
            {key: attempt.get(key) for key in ("stage", "served_provider", "served_model")}
            for attempt in cumulative
            if attempt.get("served_model") or attempt.get("served_provider")
        ]
        result["provenance"] = {
            "baseline_commit": "96ff9cf",
            "prompt_hashes": prompt_hashes(cumulative),
            "variants": dict(result["variants"]),
            "provider": provider_name,
            "requested_model": model,
            "served": served,
            "budgets": {
                "discovery_max_output_tokens": discovery_max_output_tokens,
                "selection_max_output_tokens": selection_max_output_tokens,
                "normalize_max_output_tokens": normalize_max_output_tokens,
                "translate_max_output_tokens": translate_max_output_tokens,
            },
            "source_hash": source_hash,
        }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="eval/knowledge/book-reviewed.json")
    parser.add_argument("--chapter", type=int, default=1)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--translation-model", help="optional model used only for translation")
    parser.add_argument(
        "--stage", choices=("all", "extract", "discover", "select", "normalize", "translate"), default="extract"
    )
    parser.add_argument("--reuse", help="prior artifact whose matching completed stages may be reused")
    parser.add_argument("--output", required=True)
    parser.add_argument("--discovery-max-output-tokens", type=int, default=4096)
    parser.add_argument("--selection-max-output-tokens", type=int, default=4096)
    parser.add_argument("--normalize-max-output-tokens", type=int, default=4096)
    parser.add_argument("--translate-max-output-tokens", type=int, default=4096)
    parser.add_argument(
        "--cached-input-discount", type=float,
        help="fractional discount for cache-read input; defaults to 0.5 for Groq and 0 otherwise",
    )
    parser.add_argument(
        "--reasoning-effort", default="low",
        help="reasoning effort for discover/select/normalize/translate calls; not every model accepts every value",
    )
    parser.add_argument("--discovery-variant", choices=tuple(CLAIM_CEILINGS), default="atomic")
    parser.add_argument("--selection-variant", choices=("none", "compact"), default="compact",
                        help="compact selects reader memory; none preserves broad comparison runs")
    parser.add_argument("--normalization-variant", choices=NORMALIZATION_VARIANTS, default="assertion")
    return parser


async def _main(args: argparse.Namespace) -> int:
    cfg = Config.load()
    provider_name = args.provider or cfg.llm_provider
    model = args.model or cfg.llm_model_extract
    cfg = replace(cfg, llm_provider=provider_name, llm_model_extract=model)
    provider = provider_from_env(cfg)
    reuse = json.loads(Path(args.reuse).read_text()) if args.reuse else None
    discount = args.cached_input_discount
    if discount is None:
        discount = 0.5 if provider_name == "groq" else 0.0
    if not 0 <= discount <= 1:
        raise ValueError("--cached-input-discount must be between 0 and 1")
    try:
        report = await run_experiment(
            provider,
            provider_name=provider_name,
            model=model,
            translation_model=args.translation_model,
            dataset_path=args.dataset,
            chapter=args.chapter,
            stage=args.stage,
            reuse_artifact=reuse,
            discovery_max_output_tokens=args.discovery_max_output_tokens,
            selection_max_output_tokens=args.selection_max_output_tokens,
            normalize_max_output_tokens=args.normalize_max_output_tokens,
            translate_max_output_tokens=args.translate_max_output_tokens,
            cached_input_discount=discount,
            reasoning_effort=args.reasoning_effort,
            discovery_variant=args.discovery_variant,
            normalization_variant=args.normalization_variant,
            selection_variant=args.selection_variant,
        )
    finally:
        await provider.aclose()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "status": report["status"], "output": str(output), **report["run_metrics"]
    }, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main(_parser().parse_args())))


if __name__ == "__main__":
    main()
