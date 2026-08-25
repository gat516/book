package main

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func integrationStore(t *testing.T) *Store {
	t.Helper()
	databaseURL := os.Getenv("INGEST_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("INGEST_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		t.Fatalf("ping: %v", err)
	}
	t.Cleanup(pool.Close)
	return &Store{db: pool}
}

func seedNovelWithGlossary(t *testing.T, store *Store, terms map[string]string) string {
	t.Helper()
	ctx := context.Background()
	novelID := uuid.New().String()
	_, err := store.db.Exec(ctx,
		`INSERT INTO novel (id, title, source_lang, target_lang, ontology) VALUES ($1, 'Test', 'zh', 'en', '{}')`,
		novelID,
	)
	if err != nil {
		t.Fatalf("seed novel: %v", err)
	}
	version := 0
	for source, target := range terms {
		version++
		if _, err := store.db.Exec(ctx,
			`INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter)
			 VALUES ($1, $2, $3, $4, 1)`,
			novelID, source, target, version,
		); err != nil {
			t.Fatalf("seed glossary: %v", err)
		}
	}
	t.Cleanup(func() {
		store.db.Exec(context.Background(), `DELETE FROM glossary_changelog WHERE novel_id = $1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM glossary WHERE novel_id = $1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM novel WHERE id = $1`, novelID)
	})
	return novelID
}

func TestCorrectGlossaryTermUsesNovelWideVersionCounter(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	// Two terms already at versions 1 and 2 — correcting the FIRST term must produce
	// version 3 (MAX(version)+1 across the whole novel), not 2 (this row's version+1).
	// This is the one detail a naive per-row port would get wrong.
	novelID := seedNovelWithGlossary(t, store, map[string]string{
		"青云宗": "Azure Cloud Sect",
		"凌峰":  "Ling Feng",
	})

	version, err := store.CorrectGlossaryTerm(ctx, novelID, "青云宗", "Verdant Cloud Sect", 42)
	if err != nil {
		t.Fatalf("correct: %v", err)
	}
	if version != 3 {
		t.Fatalf("version = %d, want 3 (novel-wide MAX+1, not per-row+1)", version)
	}

	var target string
	if err := store.db.QueryRow(ctx,
		"SELECT target_term FROM glossary WHERE novel_id = $1 AND source_term = $2", novelID, "青云宗",
	).Scan(&target); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if target != "Verdant Cloud Sect" {
		t.Fatalf("target_term = %q, want %q", target, "Verdant Cloud Sect")
	}
}

func TestCorrectGlossaryTermChainsChangelog(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, map[string]string{"青云宗": "Azure Cloud Sect"})

	if _, err := store.CorrectGlossaryTerm(ctx, novelID, "青云宗", "Verdant Cloud Sect", 10); err != nil {
		t.Fatalf("first correction: %v", err)
	}
	if _, err := store.CorrectGlossaryTerm(ctx, novelID, "青云宗", "Emerald Cloud Sect", 20); err != nil {
		t.Fatalf("second correction: %v", err)
	}

	rows, err := store.db.Query(ctx,
		`SELECT seq, old_target, new_target, prev_hash, row_hash FROM glossary_changelog
		 WHERE novel_id = $1 ORDER BY seq`, novelID)
	if err != nil {
		t.Fatalf("query changelog: %v", err)
	}
	defer rows.Close()

	type entry struct {
		seq                  int
		oldTarget, newTarget string
		prevHash, rowHash    string
	}
	var entries []entry
	for rows.Next() {
		var e entry
		var prevHash *string
		if err := rows.Scan(&e.seq, &e.oldTarget, &e.newTarget, &prevHash, &e.rowHash); err != nil {
			t.Fatalf("scan: %v", err)
		}
		if prevHash != nil {
			e.prevHash = *prevHash
		}
		entries = append(entries, e)
	}
	if len(entries) != 2 {
		t.Fatalf("changelog entries = %d, want 2", len(entries))
	}
	if entries[0].seq != 1 || entries[1].seq != 2 {
		t.Fatalf("seqs = %d,%d, want 1,2", entries[0].seq, entries[1].seq)
	}
	if entries[0].oldTarget != "Azure Cloud Sect" || entries[0].newTarget != "Verdant Cloud Sect" {
		t.Fatalf("entry 0 = %+v", entries[0])
	}
	if entries[1].oldTarget != "Verdant Cloud Sect" || entries[1].newTarget != "Emerald Cloud Sect" {
		t.Fatalf("entry 1 = %+v", entries[1])
	}
	if entries[1].prevHash != entries[0].rowHash {
		t.Fatalf("chain broken: entry 1 prev_hash = %q, want entry 0 row_hash = %q", entries[1].prevHash, entries[0].rowHash)
	}
}

func TestCorrectGlossaryTermNotFound(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, map[string]string{})

	_, err := store.CorrectGlossaryTerm(ctx, novelID, "不存在", "Nonexistent", 1)
	if !errors.Is(err, ErrGlossaryTermNotFound) {
		t.Fatalf("err = %v, want ErrGlossaryTermNotFound", err)
	}
}
