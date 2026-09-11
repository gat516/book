"""Stable prompts for production record discovery and identity resolution."""

DISCOVERY_SYSTEM = """Read this chapter and emit only explicit source-grounded typed records as XML.
Use exactly these types: EVENT(what,who,outcome,told), SPEECH(speaker,act,addressee,content,accepted),
STATE(character,goal,knows,unknown,condition,location), RELATION(side_a,side_b,kind,polarity),
PROMISE(promiser,promisee,promised,status), ABILITY(character,ability,effect), WORLD(fact,scope),
IDENTITY(entity,is,of). Leave inapplicable fields empty. Preserve attribution, negation, conditions,
intentions, and prior events (set told=earlier). Do not infer unstated subjects or outcomes. Cite every
supporting passage and emit at most 30 records. Return XML only:
<records><record type=\"TYPE\" evidence=\"p001,p002\"><field>value</field></record></records>"""

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
