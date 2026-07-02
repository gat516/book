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

// SourceMeta carries provenance. raw_hash drives dedup + cache keys; site_chapter_no is
// metadata only and is NEVER used as the gate key (§3.1).
type SourceMeta struct {
	SourceURL     string `json:"source_url,omitempty"`
	SiteChapterNo string `json:"site_chapter_no,omitempty"`
	FetchedAt     string `json:"fetched_at"` // RFC3339
	RawHash       string `json:"raw_hash"`   // "sha256:..." of raw_text
	Adapter       string `json:"adapter"`    // "scrape" | "paste"
}

// QueueMessage is the lightweight pointer LPUSHed onto Redis jobs:pending. We keep the
// big text blob in the object store and pass only identifiers here — the pipeline loads
// the body from chapter.raw_uri when it picks the job up.
type QueueMessage struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
}
