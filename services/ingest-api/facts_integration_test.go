package main

import (
	"context"
	"errors"
	"testing"
)

func TestRetractFactRecordsOnceAndRefusesUnknownFacts(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novel := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novel, 1, "retract-1")
	if _, err := store.db.Exec(ctx, `INSERT INTO chapter_fact
		(novel_id, chapter_index, prompt_version, ordinal, text, category, source_hash, requested_model)
		VALUES ($1, 1, 'tagged-facts-v2.txt', 0, 'A fact.', 'event', 'h', 'm')`, novel); err != nil {
		t.Fatalf("seed fact: %v", err)
	}
	req := factRetractionRequest{ChapterIndex: 1, PromptVersion: "tagged-facts-v2.txt", Ordinal: 0, Actor: "reader-1"}
	for i := 0; i < 2; i++ {
		if err := store.retractFact(ctx, novel, req); err != nil {
			t.Fatalf("retract #%d: %v", i+1, err)
		}
	}
	var count int
	if err := store.db.QueryRow(ctx, `SELECT count(*) FROM fact_retraction WHERE novel_id=$1`, novel).Scan(&count); err != nil || count != 1 {
		t.Fatalf("retractions = %d, %v; want one", count, err)
	}
	var text string
	if err := store.db.QueryRow(ctx, `SELECT text FROM chapter_fact WHERE novel_id=$1`, novel).Scan(&text); err != nil || text != "A fact." {
		t.Fatalf("the fact itself changed: %q, %v", text, err)
	}
	req.Ordinal = 9
	if err := store.retractFact(ctx, novel, req); !errors.Is(err, ErrFactNotFound) {
		t.Fatalf("unknown fact: %v; want ErrFactNotFound", err)
	}
}
