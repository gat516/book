"""Evidence-backed, revision-scoped chapter actions (spec §0, §5).

Actions are deliberately independent of global identity.  A cited surface can describe
what happened even when no safe ``entity.id`` exists yet; an optional link is accepted
only when an already verified occurrence binding is offered to the model.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sys
import time
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from pipeline.evidence import digest, passage, stable_id
from pipeline.llm.ollama import OllamaProvider
from pipeline.llm.provider import Class
from pipeline.passages import PassageContract


EVENT_PROMPT_VERSION = "chapter-events-v12-grouped-extraction"
PROMPT_HARD_BYTES = 42 * 1024
MAX_EVENTS_PER_BATCH = 10
MAX_EVENTS_PER_CHAPTER = 24
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# Stored verbatim on each event revision.  These are data, not branching application
# logic (§0.4); a later genre-specific revision may replace the complete document.
DEFAULT_EVENT_SCHEMA = {
    "types": [
        {"name": "conflict", "group": "physical_state", "description": "A fight, attack, threat, defeat, or restraint.",
         "roles": ["actor", "target", "instrument", "companion", "location"],
         "required_roles": ["actor", "target"]},
        {"name": "acquisition", "group": "possession_item", "description": "Someone obtains, takes, captures, or loses something.",
         "roles": ["actor", "item", "source", "location"], "required_roles": ["actor", "item"]},
        {"name": "transfer", "group": "possession_item", "description": "Something is given, sold, returned, or entrusted.",
         "roles": ["giver", "recipient", "item", "location"],
         "required_roles": ["giver", "recipient", "item"]},
        {"name": "use", "group": "physical_state", "description": "A technique, ability, artifact, medicine, or other item is used.",
         "roles": ["actor", "item", "target", "instrument", "location"],
         "required_roles": ["actor", "item"]},
        {"name": "communication", "group": "social_information", "description": "Consequential information, an order, promise, or warning is communicated.",
         "roles": ["actor", "target", "subject", "recipient"],
         "required_roles": ["actor", "subject"]},
        {"name": "social_interaction", "group": "social_information", "description": "An insult, offense, betrayal, rescue, alliance act, or other consequential social act.",
         "roles": ["actor", "target", "companion", "subject"],
         "required_roles": ["actor", "target"]},
        {"name": "decision", "group": "social_information", "description": "A character makes a consequential choice or commitment.",
         "roles": ["actor", "subject", "target", "item"], "required_roles": ["actor", "subject"]},
        {"name": "discovery", "group": "social_information", "description": "Someone learns, finds, identifies, or reveals something.",
         "roles": ["actor", "subject", "item", "location"], "required_roles": ["actor"],
         "required_any_role": ["subject", "item"]},
        {"name": "movement", "group": "physical_state", "description": "Consequential arrival, departure, pursuit, escape, or confinement.",
         "roles": ["actor", "source", "target", "location", "companion"],
         "required_roles": ["actor"]},
        {"name": "condition_change", "group": "physical_state", "description": "Injury, healing, death, awakening, transformation, or another major condition change.",
         "roles": ["actor", "target", "subject", "instrument"], "required_roles": ["target"]},
        {"name": "creation_destruction", "group": "physical_state", "description": "Something significant is created, repaired, damaged, or destroyed.",
         "roles": ["actor", "target", "item", "instrument", "location"],
         "required_roles": [], "required_any_role": ["target", "item"]},
    ]
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventParticipants(Strict):
    actor: str | None = None
    target: str | None = None
    instrument: str | None = None
    companion: str | None = None
    location: str | None = None
    item: str | None = None
    source: str | None = None
    giver: str | None = None
    recipient: str | None = None
    subject: str | None = None


class EventProposal(Strict):
    event_type: NonEmpty
    action: NonEmpty = Field(
        max_length=80,
        description="Short target-language verb phrase describing the action; never a participant name.",
    )
    status: Literal["completed", "attempted", "prevented"]
    participants: EventParticipants = Field(
        description="Every key is required. Copy exact cited text for applicable roles; otherwise null."
    )
    result: str | None = Field(description="Directly stated outcome in the target language, or null.")
    summary: NonEmpty = Field(
        max_length=240,
        description="One target-language sentence supported entirely by the cited passage.",
    )
    # Evidence must retain byte-for-byte whitespace because the offset is authoritative.
    # Using ``NonEmpty`` here would strip a window's leading space without moving it.
    quote: str = Field(min_length=1)
    evidence_start: int = Field(ge=0)


class EventProposals(Strict):
    events: list[EventProposal] = Field(max_length=MAX_EVENTS_PER_BATCH)


MODAL_MARKERS = (
    " would ", " could ", " might ", " may ", " if ", " unless ",
    " likely ", " no chance ", " as if ", " intent on ",
)


def role_requirements(schema: dict) -> dict[str, list[set[str]]]:
    result = {}
    for row in schema.get("types", []):
        base = set(row.get("required_roles", []))
        any_role = row.get("required_any_role", [])
        result[row["name"]] = [base | {role} for role in any_role] if any_role else [base]
    return result


def canonical_summary(item: dict) -> str:
    """Render only the validated action and roles; never persist model-added entailments."""
    roles = {argument["role"]: argument["surface"] for argument in item["arguments"]}
    action = item["action"]
    templates = {
        "conflict": lambda: f"{roles['actor']} {action} {roles['target']}.",
        "acquisition": lambda: f"{roles['actor']} {action} {roles['item']}.",
        "transfer": lambda: f"{roles['giver']} {action} {roles['item']} to {roles['recipient']}.",
        "use": lambda: f"{roles['actor']} {action} {roles['item']}" +
                       (f" on {roles['target']}" if roles.get("target") else "") + ".",
        "communication": lambda: f"{roles['actor']} {action}: {roles['subject']}.",
        "social_interaction": lambda: f"{roles['actor']} {action} {roles['target']}.",
        "decision": lambda: f"{roles['actor']} {action}: {roles['subject']}.",
        "discovery": lambda: f"{roles['actor']} {action} {roles.get('subject') or roles['item']}.",
        "movement": lambda: f"{roles['actor']} {action}" +
                            (f" to {roles['location']}" if roles.get("location") else "") + ".",
        "condition_change": lambda: f"{roles['target']} {action}.",
        "creation_destruction": lambda: f"{roles.get('actor', 'Someone')} {action} "
                                        f"{roles.get('target') or roles['item']}.",
    }
    return templates[item["event_type"]]()[:240]


def event_types(schema: dict) -> dict[str, set[str]]:
    return {
        row["name"]: set(row.get("roles", []))
        for row in schema.get("types", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }


def validate_events(
    source: str,
    proposals: EventProposals,
    schema: dict,
    linked_mentions: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Apply literal/schema checks before a proposal can enter a review report."""
    allowed = event_types(schema)
    requirements = role_requirements(schema)
    accepted, rejected = [], []
    for index, proposal in enumerate(proposals.events):
        item = dict(id=f"event:{index}", **proposal.model_dump())
        why = None
        ev = passage(source, proposal.quote, start=proposal.evidence_start)
        if not ev:
            why = "event evidence is absent or changed"
        elif proposal.event_type not in allowed:
            why = "event type is outside revision schema"
        elif proposal.action in {
            surface for surface in proposal.participants.model_dump().values() if surface
        }:
            why = "event action is a participant rather than a verb phrase"
        else:
            valid_arguments = []
            offered_by_surface = {
                offered["surface"]: mention_id for mention_id, offered in linked_mentions.items()
            }
            participants = item.pop("participants")
            literal = {
                role: surface for role, surface in participants.items()
                if surface is not None and surface.strip() and surface in proposal.quote
            }
            for role, surface in literal.items():
                if role not in allowed[proposal.event_type]:
                    continue
                valid_arguments.append({
                    "role": role, "surface": surface,
                    "mention_id": offered_by_surface.get(surface),
                })
            item["arguments"] = valid_arguments
            if not valid_arguments:
                why = "event has no schema-valid literal arguments"
            else:
                roles = {argument["role"] for argument in valid_arguments}
                alternatives = requirements.get(proposal.event_type, [])
                if alternatives and not any(required <= roles for required in alternatives):
                    why = "event is missing required participant roles"
                # Completion is the dangerous error: modal or conditional language is
                # not evidence that an in-story state transition actually occurred (§0.2).
                haystack = f" {proposal.action} {proposal.result or ''} ".lower()
                if proposal.status == "completed" and any(marker in haystack for marker in MODAL_MARKERS):
                    why = "completed event is hypothetical or conditional"
                if not why:
                    item["summary"] = canonical_summary(item)
                    # A free-form result creates a second, weakly constrained claim. Until
                    # result extraction has its own literal contract, the event is the result.
                    item["result"] = None
        (rejected if why else accepted).append(
            dict(item=item, rejection=why) if why else item
        )
    return accepted, rejected


def deduplicate_events(items: list[dict]) -> list[dict]:
    """Collapse only same-participant records supported by overlapping evidence."""
    kept: list[dict] = []
    for item in sorted(items, key=lambda row: row["evidence_start"]):
        args = tuple(sorted((a["role"], a["surface"]) for a in item["arguments"]))
        key = (item["event_type"], item["status"], args)
        start, end = item["evidence_start"], item["evidence_start"] + len(item["quote"])
        duplicate = False
        for prior in kept:
            prior_args = tuple(sorted((a["role"], a["surface"]) for a in prior["arguments"]))
            prior_key = (prior["event_type"], prior["status"], prior_args)
            prior_start = prior["evidence_start"]
            prior_end = prior_start + len(prior["quote"])
            if key == prior_key and start < prior_end and prior_start < end:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept[:MAX_EVENTS_PER_CHAPTER]


class EventEngine:
    def __init__(self, db, cfg, revision: dict):
        self.db, self.cfg, self.revision = db, cfg, revision
        if revision["prompt_version"] != EVENT_PROMPT_VERSION:
            raise ValueError("event prompt changed; create a new event revision")
        if revision["model"].get("provider") != "ollama":
            raise ValueError("event extraction permits only the pinned local Ollama model")
        self.model = revision["model"]["name"]
        runtime = revision["model"]["identity"]
        limits = cfg.graph_ollama_first_token_seconds, cfg.graph_ollama_timeout_seconds
        if None in limits:
            raise ValueError("event extraction requires configured graph Ollama timeouts")
        self.provider = OllamaProvider(
            host=cfg.ollama_host,
            model=self.model,
            first_token_timeout=limits[0],
            timeout=limits[1],
            total_timeout=cfg.graph_ollama_total_timeout_seconds,
            num_ctx=runtime["num_ctx"],
            num_predict=runtime["num_predict"],
            stream=True,
        )

    async def _call(self, stage: str, internal_schema, payload: dict, passage_ids: set[str]):
        call_event_schema = payload.get("event_schema", self.revision["event_schema"])
        contract = PassageContract(
            payload.pop("source"), passage_ids, max_chars=1600, overlap=200, windows=True,
        )
        prompt_payload = contract.prompt_payload(payload)
        extract_instructions = (
            "Extract only plot-significant events asserted as happening in these novel passages. "
            "Use ONLY the supplied event types and roles; they are one focused event group. Review all "
            "passages for that group. Distinguish completed actions from attempts "
            "and actions explicitly prevented. Exclude plans, desires, hypotheticals, negations, "
            "generic exposition, scenery, emotions, facial expressions, voice qualities, and routine "
            "speech tags. PRIORITIZE concrete answers a reader asks about: fights and injuries; insults, "
            "offenses, rescues, and betrayals; obtaining, giving, storing, consuming, or using an item; "
            "major decisions, discoveries, arrivals, escapes, and consequential warnings. Extract each "
            "distinct ownership or use step separately (for example uprooted, handed over, then pocketed). "
            "Classify plucking, uprooting, taking, receiving, pocketing, or storing an item as acquisition "
            "with actor+item. Classify handing or giving as transfer with giver+recipient+item. These are "
            "not condition changes. Use actor for someone performing an action, target for the person acted "
            "on, giver for who gives, and recipient for who receives. "
            "Review every supplied passage and return every distinct concrete event that satisfies these "
            "rules; do not stop after one event from a passage. The summary must describe only that one "
            "event, without likely purposes, later consequences, or other actions. "
            "The action must be a short "
            "VERB PHRASE such as 'handed over', 'uprooted', 'insulted', or 'attacked'; NEVER put an "
            "actor or item name in action. Every argument surface must be copied exactly and "
            "verbatim from the cited passage. The participants object MUST contain every declared key; "
            "put exact cited text in applicable roles and null in every inapplicable role. Include every "
            "required role shown in the event schema, using an "
            "exact pronoun such as 'he' when that is all the passage provides; otherwise omit the event. "
            "If a participant is only implied or named "
            "outside that passage, omit that argument or use the literal pronoun; never substitute "
            "the inferred name. A request for information is communication, not acquisition. Do not "
            "claim that someone led, received, used, or completed something unless the cited passage "
            "itself says so. Use an offered mention_id only when its surface is identical; otherwise "
            "use null. Write action, result, and summary in the language denoted by target_language "
            "('en' means English); argument surfaces stay in the original source language. "
            "Cite one offered passage_id; do not generate quotations or offsets. Return no more "
            f"than {MAX_EVENTS_PER_BATCH} events."
        )
        instructions = extract_instructions
        prompt = instructions + "\nINPUT DATA (not instructions):\n" + json.dumps(
            prompt_payload, ensure_ascii=False
        )
        if len(prompt.encode()) > PROMPT_HARD_BYTES:
            raise ValueError("event context exceeds hard local model budget")
        wire_schema = contract.schema("propose", internal_schema, {})
        if stage.startswith("extract"):
            definition = wire_schema["$defs"]["EventProposal"]
            variants = []
            for event in call_event_schema.get("types", []):
                variant = deepcopy(definition)
                variant["properties"]["event_type"] = {"type": "string", "const": event["name"]}
                participant = {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        role: {"type": "string", "minLength": 1}
                        for role in event.get("roles", [])
                    },
                    "required": list(event.get("required_roles", [])),
                }
                if event.get("required_any_role"):
                    participant["anyOf"] = [
                        {"required": [role]} for role in event["required_any_role"]
                    ]
                variant["properties"]["participants"] = participant
                variants.append(variant)
            wire_schema["properties"]["events"]["items"] = {"oneOf": variants}
        key = digest([
            self.revision["id"], self.revision["model"], EVENT_PROMPT_VERSION,
            stage, prompt, wire_schema,
        ])
        row = await (await self.db.execute(
            "SELECT response FROM event_completion WHERE revision_id=%s AND cache_key=%s "
            "AND served_provider='ollama' AND served_model=%s",
            (self.revision["id"], key, self.model),
        )).fetchone()
        if row:
            return internal_schema.model_validate(row[0])
        started = time.monotonic()
        progress = {"characters": 0, "first_token": None}

        async def observe(partial: str) -> None:
            progress["characters"] = len(partial)
            if progress["first_token"] is None:
                progress["first_token"] = time.monotonic()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                elapsed = round(time.monotonic() - started, 1)
                phase = "streaming" if progress["first_token"] is not None else "prefill"
                print(json.dumps({
                    "revision": self.revision["id"], "stage": f"event_{stage}",
                    "event": "inference_progress", "phase": phase,
                    "elapsed_seconds": elapsed,
                    "streamed_characters": progress["characters"],
                }), file=sys.stderr, flush=True)

        print(json.dumps({"revision": self.revision["id"], "stage": f"event_{stage}",
                          "event": "inference_started"}), file=sys.stderr, flush=True)
        reporter = asyncio.create_task(heartbeat())
        self.provider.stream_sink = observe
        try:
            response = await self.provider.complete(
                prompt, json_schema=wire_schema, cls=Class.BATCH, model=self.model, pin_model=True
            )
        finally:
            self.provider.stream_sink = None
            reporter.cancel()
            try:
                await reporter
            except asyncio.CancelledError:
                pass
        print(json.dumps({
            "revision": self.revision["id"], "stage": f"event_{stage}",
            "event": "inference_finished", "elapsed_seconds": round(time.monotonic() - started, 1),
            "streamed_characters": progress["characters"],
        }), file=sys.stderr, flush=True)
        if response.served_provider != "ollama" or response.served_model != self.model:
            raise RuntimeError("event serving identity changed")
        body = json.loads(response.text)
        parsed = contract.materialize("propose", internal_schema, body, {})
        await self.db.execute(
            "INSERT INTO event_completion(revision_id,cache_key,served_provider,served_model,response,"
            "elapsed_seconds,runtime_metrics) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (self.revision["id"], key, response.served_provider, response.served_model,
             Jsonb(parsed.model_dump()), time.monotonic() - started,
             Jsonb(dict(response.timings, input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens))),
        )
        return parsed

    async def linked_mentions(self, novel: str, chapter: int) -> list[dict]:
        rows = await (await self.db.execute(
            """SELECT d.id::text,d.phrase,b.entity_id::text,d.revision_id::text
                 FROM display_mention d
                 JOIN LATERAL (
                   SELECT entity_id FROM mention_binding
                    WHERE revision_id=d.revision_id AND mention_id=d.mention_id
                      AND known_from_chapter<=%s
                    ORDER BY known_from_chapter DESC LIMIT 1
                 ) b ON true
                 JOIN graph_revision r ON r.id=d.revision_id
                 JOIN novel n ON n.id=d.novel_id AND n.active_graph_revision=r.id
                WHERE d.novel_id=%s AND d.chapter_index=%s
                  AND r.state='active' AND r.trusted""",
            (chapter, novel, chapter),
        )).fetchall()
        return [dict(mention_id=mid, surface=surface, entity_id=eid,
                     graph_revision_id=rid) for mid, surface, eid, rid in rows]

    async def extract(self, novel: str, chapter: int, source: str, target_language: str) -> dict:
        linked = await self.linked_mentions(novel, chapter)
        linked_by_id = {row["mention_id"]: row for row in linked}
        contract = PassageContract(source, max_chars=1600, overlap=200, windows=True)
        # One ordinary chapter fits one batch. Long chapters are split without adding a
        # distinct event stage; each call still uses the same extraction contract.
        batches, current, size = [], [], 0
        for p in contract.passages:
            cost = len(json.dumps({"id": p["id"], "text": p["text"]}, ensure_ascii=False).encode()) + 2
            if current and (size + cost > 12000 or len(current) >= 8):
                batches.append(set(current)); current, size = [], 0
            current.append(p["id"]); size += cost
        if current:
            batches.append(set(current))
        accepted: list[dict] = []
        rejected: list[dict] = []
        groups: dict[str, list[dict]] = {}
        for event in self.revision["event_schema"].get("types", []):
            groups.setdefault(event.get("group", "general"), []).append(event)
        for batch in batches:
            available = [row for row in linked if any(
                p["id"] in batch and row["surface"] in p["text"] for p in contract.passages
            )]
            for group, types in groups.items():
                group_schema = {"types": types}
                result = await self._call(f"extract_{group}", EventProposals, dict(
                    source=source, event_schema=group_schema, target_event_group=group,
                    target_language=target_language, linked_mentions=available,
                ), batch)
                batch_accepted, batch_rejected = validate_events(
                    source, result, self.revision["event_schema"], linked_by_id,
                )
                accepted.extend(batch_accepted)
                rejected.extend(batch_rejected)
        accepted = deduplicate_events(accepted)
        # The resource budget permits one generation pass, not a second verifier call.
        # Literal passage reconstruction, schema/role checks, exact argument surfaces,
        # deduplication, and the required human revision review form the fail-closed
        # boundary. Nothing is reader-visible before that review activates the revision.
        return {"events": accepted, "rejected": rejected, "source_hash": digest(source)}

    async def publish(self, novel: str, chapter: int, source: str, output: dict) -> None:
        linked = {row["mention_id"]: row for row in await self.linked_mentions(novel, chapter)}
        revision = self.revision["id"]
        for item in output["events"]:
            ev = passage(source, item["quote"], start=item["evidence_start"])
            if not ev:
                raise ValueError("event evidence changed before publication")
            evidence_id = stable_id(revision, chapter, digest(source), ev["char_start"], ev["char_end"])
            await self.db.execute(
                """INSERT INTO event_evidence
                   (id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (evidence_id, revision, novel, chapter, digest(source), ev["char_start"],
                 ev["char_end"], ev["quote"]),
            )
            semantic = [item["event_type"], item["action"], item["status"],
                        [(a["role"], a["surface"]) for a in item["arguments"]],
                        item.get("result"), item["summary"]]
            claim_key = digest([chapter, semantic])
            event_id = stable_id(revision, "event", chapter, claim_key)
            await self.db.execute(
                """INSERT INTO chapter_event
                   (id,revision_id,novel_id,chapter_index,event_type,action,status,summary,result,evidence_id,claim_key)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (event_id, revision, novel, chapter, item["event_type"], item["action"],
                 item["status"], item["summary"], item.get("result"), evidence_id, claim_key),
            )
            for ordinal, argument in enumerate(item["arguments"]):
                mention = linked.get(argument.get("mention_id"))
                await self.db.execute(
                    """INSERT INTO chapter_event_argument
                       (revision_id,event_id,novel_id,chapter_index,ordinal,role,surface,entity_id,linked_graph_revision)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (revision, event_id, novel, chapter, ordinal, argument["role"],
                     argument["surface"], mention["entity_id"] if mention else None,
                     mention["graph_revision_id"] if mention else None),
                )

    async def close(self) -> None:
        await self.provider.aclose()
