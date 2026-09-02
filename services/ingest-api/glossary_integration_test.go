package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
		store.db.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
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

func TestBootstrapGlossaryTermUsesNovelWideVersionCounterAndLeavesEntityNull(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	// One term already locked at version 1 (as if by an earlier bootstrap call) —
	// bootstrapping a second, distinct term must produce version 2 (novel-wide MAX+1),
	// same invariant CorrectGlossaryTerm's own version-counter test exercises.
	novelID := seedNovelWithGlossary(t, store, map[string]string{"青云宗": "Azure Cloud Sect"})

	version, err := store.BootstrapGlossaryTerm(ctx, novelID, "陈枫", "Chen Feng")
	if err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	if version != 2 {
		t.Fatalf("version = %d, want 2 (novel-wide MAX+1)", version)
	}

	var target string
	var entityID *string
	var lockedAt int
	if err := store.db.QueryRow(ctx,
		"SELECT target_term, entity_id, locked_at_chapter FROM glossary WHERE novel_id = $1 AND source_term = $2",
		novelID, "陈枫",
	).Scan(&target, &entityID, &lockedAt); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if target != "Chen Feng" {
		t.Fatalf("target_term = %q, want %q", target, "Chen Feng")
	}
	if entityID != nil {
		t.Fatalf("entity_id = %v, want NULL (PLAN.md Phase N6: no entity exists until RESOLVE creates one)", *entityID)
	}
	if lockedAt != 0 {
		t.Fatalf("locked_at_chapter = %d, want 0 (locked before any chapter is read)", lockedAt)
	}
}

func TestConfirmGlossaryTermKeepsReaderKnowledgeBoundaryAndRole(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, map[string]string{})

	version, err := store.ConfirmGlossaryTerm(ctx, novelID, "契科夫", "Chekhov", 42, "foreign_person")
	if err != nil {
		t.Fatalf("confirm: %v", err)
	}
	if version != 1 {
		t.Fatalf("version = %d, want 1", version)
	}
	var target, class string
	var lockedAt, changedAt int
	if err := store.db.QueryRow(ctx, `SELECT g.target_term,g.locked_at_chapter,g.constraint_class,c.changed_at_chapter
		FROM glossary g JOIN glossary_changelog c USING(novel_id,source_term)
		WHERE g.novel_id=$1 AND g.source_term='契科夫'`, novelID).Scan(&target, &lockedAt, &class, &changedAt); err != nil {
		t.Fatal(err)
	}
	if target != "Chekhov" || lockedAt != 42 || changedAt != 42 || class != "character_name" {
		t.Fatalf("confirmed term = target %q, locked %d, changed %d, class %q", target, lockedAt, changedAt, class)
	}
}

func TestBootstrapGlossaryTermIsIdempotent(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, map[string]string{})

	first, err := store.BootstrapGlossaryTerm(ctx, novelID, "青云宗", "Azure Cloud Sect")
	if err != nil {
		t.Fatalf("first bootstrap: %v", err)
	}
	second, err := store.BootstrapGlossaryTerm(ctx, novelID, "青云宗", "Azure Cloud Sect")
	if err != nil {
		t.Fatalf("re-bootstrap with the same target: %v", err)
	}
	if second != first {
		t.Fatalf("re-bootstrap version = %d, want %d (unchanged, no-op)", second, first)
	}

	var count int
	if err := store.db.QueryRow(ctx,
		"SELECT count(*) FROM glossary_changelog WHERE novel_id = $1", novelID,
	).Scan(&count); err != nil {
		t.Fatalf("count changelog: %v", err)
	}
	if count != 1 {
		t.Fatalf("changelog rows = %d, want 1 (re-bootstrap must not append a second entry)", count)
	}
}

func TestBootstrapGlossaryTermConflictsOnDifferentTarget(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, map[string]string{})

	if _, err := store.BootstrapGlossaryTerm(ctx, novelID, "青云宗", "Azure Cloud Sect"); err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	_, err := store.BootstrapGlossaryTerm(ctx, novelID, "青云宗", "Different Name")
	if !errors.Is(err, ErrGlossaryTermConflict) {
		t.Fatalf("err = %v, want ErrGlossaryTermConflict", err)
	}
}

func TestDeleteGlossaryPreservesVersionAuditAndAllowsRecreation(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, nil)
	if version, err := store.BootstrapGlossaryTerm(ctx, novelID, "凌峰", "Ling Feng"); err != nil || version != 1 {
		t.Fatalf("create: version=%d err=%v", version, err)
	}
	version, err := store.DeleteGlossaryTerm(ctx, novelID, "凌峰", 2)
	if err != nil || version != 2 {
		t.Fatalf("delete: version=%d err=%v", version, err)
	}
	var deleted bool
	var storedVersion int
	if err := store.db.QueryRow(ctx, `SELECT deleted, version FROM glossary WHERE novel_id=$1 AND source_term='凌峰'`, novelID).Scan(&deleted, &storedVersion); err != nil || !deleted || storedVersion != 2 {
		t.Fatalf("tombstone: deleted=%v version=%d err=%v", deleted, storedVersion, err)
	}
	var oldTarget, newTarget, prevHash, rowHash string
	if err := store.db.QueryRow(ctx, `SELECT old_target, new_target, prev_hash, row_hash FROM glossary_changelog WHERE novel_id=$1 AND seq=2`, novelID).Scan(&oldTarget, &newTarget, &prevHash, &rowHash); err != nil {
		t.Fatal(err)
	}
	expected := sha256.Sum256([]byte(prevHash + pythonJSONArray(novelID, 2, "凌峰", "Ling Feng", "", 2, prevHash)))
	if oldTarget != "Ling Feng" || newTarget != "" || rowHash != hex.EncodeToString(expected[:]) {
		t.Fatal("delete audit chain is invalid")
	}
	if _, err := store.CorrectGlossaryTerm(ctx, novelID, "凌峰", "Different", 2); !errors.Is(err, ErrGlossaryTermNotFound) {
		t.Fatalf("editing a deleted term: %v", err)
	}
	// Deletion releases the target, but its version stays in the novel-wide counter.
	if version, err := store.BootstrapGlossaryTerm(ctx, novelID, "另一人", "Ling Feng"); err != nil || version != 3 {
		t.Fatalf("reuse target: %d %v", version, err)
	}
	if version, err := store.BootstrapGlossaryTerm(ctx, novelID, "凌峰", "Lingfeng"); err != nil || version != 4 {
		t.Fatalf("recreate: %d %v", version, err)
	}
	if err := store.db.QueryRow(ctx, `SELECT deleted FROM glossary WHERE novel_id=$1 AND source_term='凌峰'`, novelID).Scan(&deleted); err != nil || deleted {
		t.Fatalf("restore: %v %v", deleted, err)
	}
}
