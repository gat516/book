package main

import (
	"context"
	"testing"
)

// A provider switch must make backoff earned by the previous provider due immediately,
// or the book sits idle behind a quota it no longer uses.
func TestProviderConfigChangeReleasesDeferredChapters(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelID, 1, "sha256:deferred")
	insertTestChapter(t, store, novelID, 2, "sha256:exhausted")
	insertTestChapter(t, store, novelID, 3, "sha256:untouched")
	t.Cleanup(func() {
		store.db.Exec(context.Background(), `DELETE FROM novel_provider_config WHERE novel_id = $1`, novelID)
	})
	if _, err := store.db.Exec(ctx, `UPDATE chapter SET provider_retry_at = now() + interval '26 minutes',
		provider_retry_attempts = 1, provider_retry_category = 'quota_exhausted'
		WHERE novel_id = $1 AND chapter_index = 1`, novelID); err != nil {
		t.Fatalf("seed deferral: %v", err)
	}
	if _, err := store.db.Exec(ctx, `UPDATE chapter SET provider_retry_attempts = 5,
		provider_retry_category = 'quota_exhausted' WHERE novel_id = $1 AND chapter_index = 2`, novelID); err != nil {
		t.Fatalf("seed exhausted: %v", err)
	}

	if err := store.UpsertProviderConfig(ctx, novelID, ProviderConfigInput{
		Provider: "ollama", BaseURL: "http://127.0.0.1:11435",
	}); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	rows, err := store.db.Query(ctx, `SELECT chapter_index, provider_retry_at <= now(), provider_retry_attempts
		FROM chapter WHERE novel_id = $1 ORDER BY chapter_index`, novelID)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	defer rows.Close()
	for rows.Next() {
		var index, attempts int
		var due *bool
		if err := rows.Scan(&index, &due, &attempts); err != nil {
			t.Fatalf("scan: %v", err)
		}
		switch index {
		case 1, 2:
			if due == nil || !*due || attempts != 0 {
				t.Fatalf("chapter %d: due=%v attempts=%d, want released", index, due, attempts)
			}
		case 3:
			if due != nil {
				t.Fatalf("chapter 3 had no backoff and must stay unscheduled")
			}
		}
	}
}
