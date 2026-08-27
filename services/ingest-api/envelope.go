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
}
