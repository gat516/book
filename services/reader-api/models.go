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

// ChapterView is what the store hands back; ChapterResponse is what the handler sends.
// Kept separate so the store layer doesn't know about JSON tags.
type ChapterView struct {
	Text    string
	Spans   []SpanView
	HasNext bool
}

type ChapterResponse struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
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
