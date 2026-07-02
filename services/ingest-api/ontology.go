package main

// Ontology is the single piece of per-novel configuration that adapts the whole system
// to a genre (instructions.md §4.1). It declares the entity kinds, the attributes worth
// tracking, and the relation types. It is stored verbatim in novel.ontology (JSONB) and
// later templated into the extraction prompts. Same code, different JSON per novel.
type Ontology struct {
	Kinds      []string    `json:"kinds"`
	Attributes []Attribute `json:"attributes"`
	Relations  []string    `json:"relations"`
}

// Attribute is a per-entity, time-versioned attribute (tracked as facts).
type Attribute struct {
	Name  string   `json:"name"`
	Kinds []string `json:"kinds"`
}

// genrePresets maps novel.genre → a seed ontology (the §4.1 table). These are
// conveniences: the graph never validates against them at write time, so an extractor
// that discovers a kind a preset missed just writes it. The preset only shapes what the
// extraction prompt asks for. An unknown/empty genre falls back to genericOntology.
var genrePresets = map[string]Ontology{
	"xianxia": {
		Kinds: []string{"character", "sect", "realm", "technique", "artifact", "beast"},
		Attributes: []Attribute{
			{Name: "rank", Kinds: []string{"character"}}, // cultivation realm
			{Name: "status", Kinds: []string{"character"}},
			{Name: "affiliation", Kinds: []string{"character"}},
			{Name: "state", Kinds: []string{"sect"}},
		},
		Relations: []string{"member_of", "ally", "enemy", "mentor", "family", "located_in"},
	},
	"fantasy": {
		Kinds: []string{"character", "house", "kingdom", "location", "magic_system"},
		Attributes: []Attribute{
			{Name: "title", Kinds: []string{"character"}},
			{Name: "allegiance", Kinds: []string{"character"}},
			{Name: "status", Kinds: []string{"character"}},
		},
		Relations: []string{"member_of", "ally", "enemy", "mentor", "family", "located_in"},
	},
	"mystery": {
		Kinds: []string{"character", "suspect", "clue", "location", "organization"},
		Attributes: []Attribute{
			{Name: "suspicion", Kinds: []string{"suspect"}},
			{Name: "alibi", Kinds: []string{"suspect"}},
			{Name: "status", Kinds: []string{"character"}},
		},
		Relations: []string{"member_of", "ally", "enemy", "family", "located_in"},
	},
	"litrpg": {
		Kinds: []string{"character", "skill", "item", "quest", "guild"},
		Attributes: []Attribute{
			{Name: "level", Kinds: []string{"character"}},
			{Name: "class", Kinds: []string{"character"}},
			{Name: "stats", Kinds: []string{"character"}},
		},
		Relations: []string{"member_of", "ally", "enemy", "mentor", "family", "located_in"},
	},
	"romance": {
		Kinds: []string{"character", "family", "location", "organization"},
		Attributes: []Attribute{
			{Name: "relationship_status", Kinds: []string{"character"}},
			{Name: "occupation", Kinds: []string{"character"}},
		},
		Relations: []string{"family", "ally", "enemy", "located_in"},
	},
}

// genericOntology is the fallback used when genre is empty or unrecognized. Auto-induction
// (§4.2) will eventually replace this by proposing an ontology from the first K chapters;
// until the pipeline exists, the generic preset keeps novels ingestible.
var genericOntology = Ontology{
	Kinds: []string{"character", "group", "location", "item"},
	Attributes: []Attribute{
		{Name: "status", Kinds: []string{"character"}},
		{Name: "role", Kinds: []string{"character"}},
	},
	Relations: []string{"member_of", "ally", "enemy", "located_in"},
}

// ontologyForGenre returns the preset for a genre, or the generic fallback.
func ontologyForGenre(genre string) Ontology {
	if o, ok := genrePresets[genre]; ok {
		return o
	}
	return genericOntology
}
