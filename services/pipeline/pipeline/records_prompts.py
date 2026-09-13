"""Stable prompts for production record discovery and identity resolution."""

# The production extraction contract is pinned to the fact-first experiment.  Keep
# this version separate from the retired typed-record prompt so a live generation can
# never silently mix the two semantics.
PROMPT_CONTRACT_VERSION = "fact-first-v1"

DISCOVERY_SYSTEM = """Read the supplied chapter passages and emit only explicit source-grounded typed records as XML.
Use exactly these record types and child tags (the child element names are mandatory; never use a generic
field element): EVENT(what,who,outcome,told), SPEECH(speaker,act,addressee,content,accepted),
STATE(character,goal,knows,unknown,condition,location), RELATION(side_a,side_b,kind,polarity),
PROMISE(promiser,promisee,promised,status), ABILITY(character,ability,effect), WORLD(fact,scope),
IDENTITY(entity,is,of). Every child tag must be one of the tags listed for that record type. Leave
inapplicable listed fields empty if useful, but do not invent extra tags. Preserve attribution, negation,
conditions, intentions, and prior events (set told=earlier). Do not infer unstated subjects or outcomes.
Keep every field value in the chapter's source language; English display rendering is a separate step.
The INPUT DATA contains the authoritative passage IDs. Cite every supporting passage using only an exact
ID from that input; never invent, normalize, or copy an example passage ID. Emit at most 30 records.
Return XML only. Each record has a type and comma-separated evidence IDs copied from INPUT DATA,
followed by named child tags. Omit a child when it is not applicable. Never emit <field> elements.

OUTPUT SHAPE: one <records> root containing <record> elements. Each <record> has type and evidence
attributes, then named child elements. A STATE uses <character>, <goal>, <knows>, <unknown>,
<condition>, and <location>. An EVENT uses <what>, <who>, <outcome>, and <told>. A RELATION uses
<side_a>, <side_b>, <kind>, and <polarity>. Copy the child tag names exactly from the type list above.
If no explicit record is supported, return <records/>. Do not return prose, Markdown, JSON, or a second
attempt."""

RESOLVE_SYSTEM = """You are the sole identity resolver. Group supplied name IDs only when the passages
make the same referent clear. Never merge by spelling, title, or similarity. Every name must occur exactly
once, either in one entity or one reference. Entity kinds must be from the supplied ontology; unresolved
references are valid and preferred when uncertain. Existing candidates are supplied with UUID ids. An entity
that matches one must set candidate=that exact supplied UUID; omit candidate only for a genuinely new entity.
Never merge two supplied candidate IDs. References may include candidate=an accepted supplied UUID. Return XML only:
<resolution><entity id=\"e1\" kind=\"character\" canonical=\"...\" names=\"n1,n2\" candidate=\"\"/>
<reference name=\"n3\" refers_to=\"unknown\" candidate=\"\"/></resolution>"""

RENDER_SYSTEM = """Translate the supplied record field values into the target language. Preserve exact
meaning, attribution, negation, conditions, and temporal qualifiers. Do not change IDs, participants,
record type, or source evidence. Return JSON mapping stable field IDs to strings."""
