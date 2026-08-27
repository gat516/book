package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

// seedNovelForChapters creates an empty novel and cleans up everything keyed to it.
func seedNovelForChapters(t *testing.T, store *Store) string {
	t.Helper()
	ctx := context.Background()
	novelID := uuid.New().String()
	if _, err := store.db.Exec(ctx,
		`INSERT INTO novel (id, title, source_lang, target_lang, ontology)
		 VALUES ($1, 'Dedup Test', 'zh', 'en', '{}')`, novelID,
	); err != nil {
		t.Fatalf("seed novel: %v", err)
	}
	t.Cleanup(func() {
		store.db.Exec(context.Background(), `DELETE FROM chapter WHERE novel_id = $1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM novel WHERE id = $1`, novelID)
	})
	return novelID
}

func insertTestChapter(t *testing.T, store *Store, novelID string, index int, rawHash string) {
	t.Helper()
	if _, err := store.db.Exec(context.Background(),
		`INSERT INTO chapter (novel_id, chapter_index, raw_hash, raw_uri, source_meta, status)
		 VALUES ($1, $2, $3, $4, '{}', 'ingested')`,
		novelID, index, rawHash, "raw/test.txt",
	); err != nil {
		t.Fatalf("insert chapter %d: %v", index, err)
	}
}

func TestChapterIndexByHashFindsExistingContent(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelID, 7, "sha256:abc")

	index, found, err := store.chapterIndexByHash(ctx, novelID, "sha256:abc")
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if !found || index != 7 {
		t.Fatalf("got (index=%d, found=%v), want (7, true)", index, found)
	}

	// A body this novel has never seen must not report a false match — that would make
	// the paste path silently drop genuinely new chapters.
	if _, found, err = store.chapterIndexByHash(ctx, novelID, "sha256:unseen"); err != nil {
		t.Fatalf("lookup unseen: %v", err)
	} else if found {
		t.Fatal("unseen hash reported as already ingested")
	}
}

// The dedup is per novel: two novels legitimately may hold an identical short chapter, and
// one novel's content must never suppress another's ingestion.
func TestChapterIndexByHashIsScopedPerNovel(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelA := seedNovelForChapters(t, store)
	novelB := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelA, 1, "sha256:shared")

	if _, found, err := store.chapterIndexByHash(ctx, novelB, "sha256:shared"); err != nil {
		t.Fatalf("lookup: %v", err)
	} else if found {
		t.Fatal("novel A's content reported as already ingested for novel B")
	}
}

// Migration 0013's unique index is the backstop under the app-layer check: even if two
// concurrent pastes both pass the lookup, only one row can land. Without it, the scraper's
// fresh-index-per-run behaviour re-inserts every chapter with nothing to conflict on,
// which is exactly how 27 duplicates reached one novel.
func TestDuplicateContentAtANewIndexIsRejectedByTheDatabase(t *testing.T) {
	store := integrationStore(t)
	novelID := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelID, 1, "sha256:same")

	_, err := store.db.Exec(context.Background(),
		`INSERT INTO chapter (novel_id, chapter_index, raw_hash, raw_uri, source_meta, status)
		 VALUES ($1, $2, $3, $4, '{}', 'ingested')`,
		novelID, 2, "sha256:same", "raw/test.txt",
	)
	if err == nil {
		t.Fatal("inserting identical content at a new chapter_index succeeded; 0013's unique index is missing")
	}
}
