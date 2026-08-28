package main

import "time"

type Progress struct {
	NovelID        string    `json:"novel_id"`
	ReaderID       string    `json:"reader_id"`
	CurrentChapter int       `json:"current_chapter"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type EntitySummary struct {
	ID               string `json:"id"`
	Canonical        string `json:"canonical"`
	Kind             string `json:"kind"`
	FirstSeenChapter int    `json:"first_seen_chapter"`
}

type FactView struct {
	Attribute        string  `json:"attribute"`
	Value            string  `json:"value"`
	ValidFromChapter int     `json:"valid_from_chapter"`
	SourceChapter    int     `json:"source_chapter"`
	Confidence       float32 `json:"confidence"`
}

type EntityView struct {
	EntitySummary
	Aliases []string   `json:"aliases"`
	Facts   []FactView `json:"facts"`
}

type EventView struct {
	ID           int64           `json:"id"`
	ChapterIndex int             `json:"chapter_index"`
	Summary      string          `json:"summary"`
	Entities     []EntitySummary `json:"entities"`
}

type RelationshipView struct {
	ID               int64         `json:"id"`
	Relation         string        `json:"relation"`
	Direction        string        `json:"direction"`
	Entity           EntitySummary `json:"entity"`
	ValidFromChapter int           `json:"valid_from_chapter"`
	ValidToChapter   *int          `json:"valid_to_chapter"`
	SourceChapter    int           `json:"source_chapter"`
}

type EntityResponse struct {
	NovelID string     `json:"novel_id"`
	At      int        `json:"at"`
	Entity  EntityView `json:"entity"`
}

type WikiResponse struct {
	NovelID  string          `json:"novel_id"`
	At       int             `json:"at"`
	Entities []EntitySummary `json:"entities"`
}

type TimelineResponse struct {
	NovelID string      `json:"novel_id"`
	At      int         `json:"at"`
	Events  []EventView `json:"events"`
}

type RelationshipsResponse struct {
	NovelID       string             `json:"novel_id"`
	At            int                `json:"at"`
	EntityID      string             `json:"entity_id"`
	Relationships []RelationshipView `json:"relationships"`
}

type GlossaryTermView struct {
	EntityID        *string `json:"entity_id,omitempty"`
	SourceTerm      string  `json:"source_term"`
	TargetTerm      string  `json:"target_term"`
	Version         int     `json:"version"`
	LockedAtChapter int     `json:"locked_at_chapter"`
}

type GlossaryResponse struct {
	NovelID string             `json:"novel_id"`
	At      int                `json:"at"`
	Terms   []GlossaryTermView `json:"terms"`
}

type ScrapeJobView struct {
	ID              int64     `json:"id"`
	NovelID         string    `json:"novel_id"`
	StartURL        string    `json:"start_url"`
	Mode            string    `json:"mode"`
	Status          string    `json:"status"`
	ChaptersFetched int       `json:"chapters_fetched"`
	LastError       *string   `json:"last_error"`
	CancelRequested bool      `json:"cancel_requested"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

type NovelSummary struct {
	ID         string    `json:"id"`
	Title      string    `json:"title"`
	SourceLang string    `json:"source_lang"`
	TargetLang string    `json:"target_lang"`
	Genre      *string   `json:"genre"`
	CreatedAt  time.Time `json:"created_at"`
}

type NovelListResponse struct {
	Novels []NovelSummary `json:"novels"`
}

type SpanView struct {
	EntityID  string `json:"entity_id"`
	CharStart int    `json:"char_start"`
	CharEnd   int    `json:"char_end"`
}

// ChapterListItem is one row of the chapter index — navigation/ingestion metadata only,
// never chapter text. Status is the pipeline status ('ingested' until the worker finishes
// it, then 'done', or 'error'), which is what lets the UI say "still being translated"
// instead of bouncing off GetChapter's 404/409.
type ChapterListItem struct {
	ChapterIndex  int    `json:"chapter_index"`
	SiteChapterNo string `json:"site_chapter_no,omitempty"`
	// Part of a multi-page source chapter (1-based; 1 for an ordinary chapter). Sites that
	// paginate a chapter produce several rows sharing one SiteChapterNo, distinguished
	// only by this.
	Part   int    `json:"part"`
	Status string `json:"status"`
}

// ChapterListResponse pages the index: a scraped novel can hold thousands of chapters, so
// this is never returned unbounded. Progress is the reader's stored current_chapter (0 when
// they have none yet) so the UI can mark "you are here" without a second round trip.
type ChapterListResponse struct {
	NovelID  string            `json:"novel_id"`
	Chapters []ChapterListItem `json:"chapters"`
	Total    int               `json:"total"`
	Limit    int               `json:"limit"`
	Offset   int               `json:"offset"`
	Progress int               `json:"progress"`
}

// InFlightChapter is one chapter the pipeline worker currently holds a claim on.
// Stage is the pipeline stage running right now ("translate", "resolve", …), empty if the
// worker claimed the chapter but hasn't started a stage yet.
type InFlightChapter struct {
	ChapterIndex int    `json:"chapter_index"`
	Stage        string `json:"stage,omitempty"`
	ElapsedSecs  int    `json:"elapsed_secs"`
}

// PipelineStatusResponse answers "what is the worker actually doing right now" — the one
// question neither the chapter list nor the scrape status could answer. A single TRANSLATE
// call against a local model can run for minutes with no outward change, which is
// indistinguishable from a hung or stopped worker without this.
//
// Read from Redis (the queue the worker drains), not Postgres: chapter.status only flips
// once every stage has finished, so it is blind to work in progress by construction.
type PipelineStatusResponse struct {
	NovelID string `json:"novel_id"`
	// Pending counts the WHOLE queue, not just this novel: the worker drains one shared
	// queue, so another novel's backlog is exactly why this novel's chapters are waiting.
	Pending  int               `json:"pending"`
	InFlight []InFlightChapter `json:"in_flight"`
}

// ChapterPreviewResponse carries a chapter's translation as it is being produced.
// Available is false whenever nothing is streaming — before TRANSLATE starts, for a
// provider that can't stream, and after the chapter is finished (at which point the real
// chapter endpoint serves it).
type ChapterPreviewResponse struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
	Available    bool   `json:"available"`
	Text         string `json:"text,omitempty"`
	// Status is the chapter's pipeline status ("ingested"/"queued"/"done"/"error"), served
	// alongside the preview so a waiting client learns both "how far along is it" and "is
	// it ready" from ONE read. Previously readiness was probed by repeatedly attempting
	// PUT /progress — a write, several times a minute, to answer a read-only question.
	Status string `json:"status"`
}

// TranslationHealth reports whether this novel's terminology is being translated
// consistently. It measures the model's self-agreement, not translation quality — quality
// needs a human, but "the model proposed four different English names for one character"
// is countable, and is what a reader would want flagged.
//
// Signal comes from glossary_candidate (migration 0016): every target the model has
// proposed is recorded there until corroborated, so competing proposals for one source
// term are visible directly rather than inferred.
type TranslationHealth struct {
	NovelID string `json:"novel_id"`
	// LockedTerms have been corroborated and are enforced on every translation.
	LockedTerms int `json:"locked_terms"`
	// UnstableTerms are source terms with more than one distinct proposed target — the
	// model naming the same entity differently in different chapters.
	UnstableTerms int `json:"unstable_terms"`
	// ProvisionalTerms are distinct source terms seen but not yet corroborated. Part of
	// the sample size: without them, a model so inconsistent that nothing ever locks
	// would report almost no evidence and never be flagged.
	ProvisionalTerms int `json:"provisional_terms"`
	// FailedChapters were rejected by glossary validation, usually the same underlying
	// cause seen from the other end.
	FailedChapters int `json:"failed_chapters"`
	// Warn is the server's judgement, so every client applies the same threshold rather
	// than each inventing one. Reason is empty when Warn is false.
	Warn   bool   `json:"warn"`
	Reason string `json:"reason,omitempty"`
}

// ChapterView is what the store hands back; ChapterResponse is what the handler sends.
// Kept separate so the store layer doesn't know about JSON tags.
type ChapterView struct {
	Text          string
	Spans         []SpanView
	HasNext       bool
	SiteChapterNo string // "" when this chapter has none (a plain paste, not a scrape)
	Part          int    // 1-based; 1 for an ordinary (non-paginated) chapter
}

type ChapterResponse struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
	// SiteChapterNo is the source site's own printed chapter label (e.g. "第4610章"),
	// distinct from ChapterIndex — our own sequential counter for THIS ingestion batch,
	// not the novel's overall chapter number (instructions.md §3.1: chapter_index is the
	// gate key, site_chapter_no is inert display metadata). Omitted when absent (a plain
	// paste, not a scrape) so the reader UI can distinguish "no site label" from "".
	SiteChapterNo string `json:"site_chapter_no,omitempty"`
	// Part of a multi-page source chapter (1-based; 1 when the chapter isn't paginated).
	Part int `json:"part"`
	// At is the reader's STORED PROGRESS (not the chapter index n). Re-reading an old
	// chapter (n < progress) still uses progress here: the reader has already legitimately
	// learned everything up to it, so showing those facts on old text is not a leak — the
	// gate protects against learning the future relative to what's been read, not against
	// carrying already-learned knowledge backward. This is also the exact value the client
	// must use as the hover-card cache key's `at` (PLAN.md §5.3/§6.1).
	At      int        `json:"at"`
	Text    string     `json:"text"`
	Spans   []SpanView `json:"spans"`
	HasNext bool       `json:"has_next"`
}
