package main

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"testing"

	"github.com/redis/go-redis/v9"
)

// Extract finds missing facts: unfinished chapters resume in order, a chapter with facts
// is not re-extracted, and a chapter a worker already holds is left alone.
func TestExtractFactsResumesOnlyUnfinishedIdleChapters(t *testing.T) {
	url := os.Getenv("INGEST_TEST_REDIS_URL")
	if url == "" {
		t.Skip("INGEST_TEST_REDIS_URL is not set")
	}
	store := integrationStore(t)
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	store.redis = redis.NewClient(opts)
	t.Cleanup(func() { _ = store.redis.Close() })
	ctx := context.Background()
	novel := seedNovelForChapters(t, store)
	t.Cleanup(func() {
		for _, key := range []string{pendingQueue, "jobs:processing"} {
			entries, _ := store.redis.LRange(context.Background(), key, 0, -1).Result()
			for _, raw := range entries {
				var msg QueueMessage
				if json.Unmarshal([]byte(raw), &msg) == nil && msg.NovelID == novel {
					store.redis.LRem(context.Background(), key, 0, raw)
				}
			}
		}
	})
	for i := 1; i <= 5; i++ {
		insertTestChapter(t, store, novel, i, "extract-"+string(rune('0'+i)))
	}
	for _, sql := range []string{
		`UPDATE chapter SET translation_ready = chapter_index <> 5 WHERE novel_id=$1`,
		// 1 done (has facts), 2 failed with an automatic retry, 3 paused by a discard.
		`UPDATE chapter SET facts_count=3 WHERE novel_id=$1 AND chapter_index=1`,
		`UPDATE chapter SET enrichment_attempts=1, enrichment_retry_at=now()+interval '5 minutes'
		 WHERE novel_id=$1 AND chapter_index=2`,
		`UPDATE chapter SET enrichment_discarded=true WHERE novel_id=$1 AND chapter_index=3`,
	} {
		if _, err := store.db.Exec(ctx, sql, novel); err != nil {
			t.Fatalf("seed %q: %v", sql, err)
		}
	}
	// 4 is mid-chapter on a worker.
	claimed, _ := json.Marshal(QueueMessage{NovelID: novel, ChapterIndex: 4, Enrichment: true})
	if err := store.redis.LPush(ctx, "jobs:processing", claimed).Err(); err != nil {
		t.Fatal(err)
	}

	enqueued, err := store.extractFacts(ctx, novel)
	if err != nil {
		t.Fatalf("extract: %v", err)
	}
	if enqueued != 2 {
		t.Fatalf("enqueued %d chapters, want 2 (chapters 2 and 3)", enqueued)
	}
	pending, _ := store.redis.LRange(ctx, pendingQueue, 0, -1).Result()
	got := []int{}
	for i := len(pending) - 1; i >= 0; i-- { // LPUSH: oldest at the tail
		var msg QueueMessage
		if json.Unmarshal([]byte(pending[i]), &msg) == nil && msg.NovelID == novel {
			if !msg.Enrichment || msg.Priority {
				t.Fatalf("pointer %s: want a non-priority enrichment pointer", pending[i])
			}
			got = append(got, msg.ChapterIndex)
		}
	}
	if !reflect.DeepEqual(got, []int{2, 3}) {
		t.Fatalf("queued chapters %v, want [2 3] in order", got)
	}
	var discarded bool
	var retryScheduled int
	if err := store.db.QueryRow(ctx, `SELECT
		bool_or(enrichment_discarded), count(enrichment_retry_at)
		FROM chapter WHERE novel_id=$1`, novel).Scan(&discarded, &retryScheduled); err != nil {
		t.Fatal(err)
	}
	if discarded || retryScheduled != 0 {
		t.Fatalf("discarded=%v retries=%d; want all cleared", discarded, retryScheduled)
	}
	status, err := store.factsStatus(ctx, novel)
	if err != nil {
		t.Fatal(err)
	}
	if !status.Running {
		t.Fatalf("status.Running=false with facts work queued")
	}
	if status.EligibleChapters != 4 || status.DoneChapters != 1 || status.MissingChapters != 3 {
		t.Fatalf("status %+v, want 4 eligible, 1 done, 3 missing", status)
	}
}

// FACTS reads only its own chapter, so a retry never waits on an earlier one.
func TestRetryFactsDoesNotWaitOnEarlierChapters(t *testing.T) {
	url := os.Getenv("INGEST_TEST_REDIS_URL")
	if url == "" {
		t.Skip("INGEST_TEST_REDIS_URL is not set")
	}
	store := integrationStore(t)
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	store.redis = redis.NewClient(opts)
	t.Cleanup(func() { _ = store.redis.Close() })
	ctx := context.Background()
	novel := seedNovelForChapters(t, store)
	t.Cleanup(func() {
		entries, _ := store.redis.LRange(context.Background(), pendingQueue, 0, -1).Result()
		for _, raw := range entries {
			var msg QueueMessage
			if json.Unmarshal([]byte(raw), &msg) == nil && msg.NovelID == novel {
				store.redis.LRem(context.Background(), pendingQueue, 0, raw)
			}
		}
	})
	for i := 1; i <= 3; i++ {
		insertTestChapter(t, store, novel, i, "order-"+string(rune('0'+i)))
	}
	if _, err := store.db.Exec(ctx, `UPDATE chapter SET translation_ready=true, facts_count = CASE
		WHEN chapter_index=1 THEN 2 END WHERE novel_id=$1`, novel); err != nil {
		t.Fatal(err)
	}
	// FACTS reads only its own chapter, so a retry no longer waits on an earlier one.
	if err := store.retryFacts(ctx, novel, 3); err != nil {
		t.Fatalf("retry chapter 3 behind failed chapter 2: %v", err)
	}
	if err := store.retryFacts(ctx, novel, 2); err != nil {
		t.Fatalf("retry chapter 2: %v", err)
	}
}
