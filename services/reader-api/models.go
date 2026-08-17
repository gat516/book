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
