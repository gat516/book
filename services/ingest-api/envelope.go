package main

// ChapterEnvelope is the normalized ingestion unit (instructions.md §3.1). Every source
// adapter (paste today, scraper later) produces one; nothing downstream knows or cares
// where a chapter came from. The paste handler builds one from the POST body.
type ChapterEnvelope struct {
	NovelID      string     `json:"novel_id"`
	ChapterIndex int        `json:"chapter_index"` // INTERNAL canonical index, not the site's number
	RawText      string     `json:"raw_text"`      // extracted source-language body, no nav/ads
	SourceLang   string     `json:"source_lang"`
	SourceMeta   SourceMeta `json:"source_meta"`
}

// SourceMeta carries provenance. raw_hash drives dedup + cache keys; site_chapter_no and
// part are metadata only and are NEVER used as the gate key (§3.1).
type SourceMeta struct {
	SourceURL     string `json:"source_url,omitempty"`
	SiteChapterNo string `json:"site_chapter_no,omitempty"`
	// Part numbers the pieces of a source chapter that the site splits across several
	// pages: sites like m.shuhaige.net serve one chapter as ~3 paginated pages, each of
	// which becomes its own chapter row here. Together (SiteChapterNo, Part) say "第4610章,
	// part 2" — which is what a reader recognises, where the internal ChapterIndex is a
	// flat count of pages ingested and matches nothing the source displays.
	//
	// 1-based, and 1 for an ordinary single-page chapter — so "part 1 unless told
	// otherwise" is the default rather than a special case anyone has to handle.
	Part      int    `json:"part,omitempty"`
	FetchedAt string `json:"fetched_at"` // RFC3339
	RawHash   string `json:"raw_hash"`   // "sha256:..." of raw_text
	Adapter   string `json:"adapter"`    // "scrape" | "paste"
}

// QueueMessage is the lightweight pointer LPUSHed onto Redis jobs:pending. We keep the
// big text blob in the object store and pass only identifiers here — the pipeline loads
// the body from chapter.raw_uri when it picks the job up.
type QueueMessage struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
	Priority     bool   `json:"priority,omitempty"`
	// Enrichment keeps an already-readable chapter out of the translation-critical
	// queue. The pipeline uses this to reuse durable prose while running facts.
	Enrichment  bool `json:"enrichment,omitempty"`
	Retranslate bool `json:"retranslate,omitempty"`
	// Respell rides on a Retranslate pointer when a confirmed name replaces a spelling
	// that was primed into the chapter: the pipeline swaps it in the saved text instead of
	// calling the model, and retranslates only when the old spelling is not there.
	Respell []Respelling `json:"respell,omitempty"`
}

// Respelling is one name whose chapter text currently reads From and should read To.
type Respelling struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// respellFor decides what a newly confirmed spelling costs an already-translated
// chapter. Names are primed into the source before translation (§0: structural, not
// prompted), so the text already holds the primed spelling verbatim: the same spelling
// needs no work at all, a different one is a literal swap. Only an unknown primed
// spelling still needs the model.
func respellFor(msg QueueMessage, primed, target string) (QueueMessage, bool) {
	if !msg.Retranslate || primed == "" {
		return msg, true
	}
	if primed == target {
		return msg, false
	}
	msg.Respell = []Respelling{{From: primed, To: target}}
	return msg, true
}
