package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"testing"

	"github.com/redis/go-redis/v9"
)

// Extract continues the active graph: unfinished chapters resume in order, a published
// chapter is not re-extracted, and a chapter a worker already holds is left alone.
func TestExtractRecordsResumesOnlyUnfinishedIdleChapters(t *testing.T) {
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
		store.db.Exec(context.Background(), `UPDATE novel SET active_record_generation=NULL WHERE id=$1`, novel)
		store.db.Exec(context.Background(), `DELETE FROM record_run WHERE novel_id=$1`, novel)
		store.db.Exec(context.Background(), `DELETE FROM record_generation WHERE novel_id=$1`, novel)
	})
	for i := 1; i <= 5; i++ {
		insertTestChapter(t, store, novel, i, "extract-"+string(rune('0'+i)))
	}
	var generation string
	if err := store.db.QueryRow(ctx, `INSERT INTO record_generation
		(novel_id,state,ontology,prompt_version,checks_version,extraction_model,source_lang,target_lang)
		VALUES ($1,'active','{}','records-v1','records-v1','','zh','en') RETURNING id::text`, novel).Scan(&generation); err != nil {
		t.Fatalf("seed generation: %v", err)
	}
	for _, sql := range []string{
		`UPDATE novel SET active_record_generation=$2 WHERE id=$1`,
		`UPDATE chapter SET translation_ready = chapter_index <> 5 WHERE novel_id=$1 AND $2::uuid IS NOT NULL`,
		// 1 published, 2 failed with an automatic retry, 3 paused by a discard.
		`INSERT INTO record_run (novel_id,generation_id,chapter_index,source_hash,request_identity,status)
		 VALUES ($1,$2,1,'h','r','published'),($1,$2,2,'h','r','failed')`,
		`UPDATE chapter SET enrichment_attempts=1, enrichment_retry_at=now()+interval '5 minutes'
		 WHERE novel_id=$1 AND chapter_index=2 AND $2::uuid IS NOT NULL`,
		`UPDATE chapter SET enrichment_discarded=true WHERE novel_id=$1 AND chapter_index=3 AND $2::uuid IS NOT NULL`,
	} {
		if _, err := store.db.Exec(ctx, sql, novel, generation); err != nil {
			t.Fatalf("seed %q: %v", sql, err)
		}
	}
	// 4 is mid-chapter on a worker.
	claimed, _ := json.Marshal(QueueMessage{NovelID: novel, ChapterIndex: 4, Enrichment: true, RecordGenerationID: generation})
	if err := store.redis.LPush(ctx, "jobs:processing", claimed).Err(); err != nil {
		t.Fatal(err)
	}

	enqueued, err := store.extractRecords(ctx, novel)
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
			if !msg.Enrichment || msg.Priority || msg.RecordGenerationID != generation {
				t.Fatalf("pointer %s: want a pinned, non-priority enrichment pointer", pending[i])
			}
			got = append(got, msg.ChapterIndex)
		}
	}
	if !reflect.DeepEqual(got, []int{2, 3}) {
		t.Fatalf("queued chapters %v, want [2 3] in order", got)
	}
	var discarded bool
	var retryScheduled, failedRuns int
	if err := store.db.QueryRow(ctx, `SELECT
		bool_or(enrichment_discarded), count(enrichment_retry_at),
		(SELECT count(*) FROM record_run WHERE novel_id=$1 AND status<>'published')
		FROM chapter WHERE novel_id=$1`, novel).Scan(&discarded, &retryScheduled, &failedRuns); err != nil {
		t.Fatal(err)
	}
	if discarded || retryScheduled != 0 || failedRuns != 0 {
		t.Fatalf("discarded=%v retries=%d unfinished runs=%d; want all cleared", discarded, retryScheduled, failedRuns)
	}
	status, err := store.recordsRebuildStatus(ctx, novel)
	if err != nil {
		t.Fatal(err)
	}
	if !status.Running {
		t.Fatalf("status.Running=false with graph work queued")
	}
}

// A chapter retry that the worker's order fence would reject is refused up front, while
// the earliest unfinished chapter can still be retried directly.
func TestRetryRecordsRefusesChapterBehindAnUnpublishedOne(t *testing.T) {
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
		store.db.Exec(context.Background(), `UPDATE novel SET active_record_generation=NULL WHERE id=$1`, novel)
		store.db.Exec(context.Background(), `DELETE FROM record_run WHERE novel_id=$1`, novel)
		store.db.Exec(context.Background(), `DELETE FROM record_generation WHERE novel_id=$1`, novel)
	})
	for i := 1; i <= 3; i++ {
		insertTestChapter(t, store, novel, i, "order-"+string(rune('0'+i)))
	}
	var generation string
	if err := store.db.QueryRow(ctx, `INSERT INTO record_generation
		(novel_id,state,ontology,prompt_version,checks_version,extraction_model,source_lang,target_lang)
		VALUES ($1,'active','{}','records-v1','records-v1','','zh','en') RETURNING id::text`, novel).Scan(&generation); err != nil {
		t.Fatal(err)
	}
	for _, sql := range []string{
		`UPDATE novel SET active_record_generation=$2 WHERE id=$1`,
		`UPDATE chapter SET translation_ready=true WHERE novel_id=$1 AND $2::uuid IS NOT NULL`,
		`INSERT INTO record_run (novel_id,generation_id,chapter_index,source_hash,request_identity,status)
		 VALUES ($1,$2,1,'h','r','published'),($1,$2,2,'h','r','failed'),($1,$2,3,'h','r','failed')`,
	} {
		if _, err := store.db.Exec(ctx, sql, novel, generation); err != nil {
			t.Fatalf("seed %q: %v", sql, err)
		}
	}
	if err := store.retryRecords(ctx, novel, 3); !errors.Is(err, ErrRecordsOutOfOrder) {
		t.Fatalf("retry chapter 3 behind failed chapter 2: err=%v, want ErrRecordsOutOfOrder", err)
	}
	if err := store.retryRecords(ctx, novel, 2); err != nil {
		t.Fatalf("retry earliest unfinished chapter: %v", err)
	}
}
