package main

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"testing"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

func TestPriorityMovesOnlyRequestedChapterAndNeverDuplicatesProcessing(t *testing.T) {
	url := os.Getenv("INGEST_TEST_REDIS_URL")
	if url == "" {
		t.Skip("INGEST_TEST_REDIS_URL is not set")
	}
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(opts)
	t.Cleanup(func() { _ = client.Close() })
	ctx := context.Background()
	prefix := "test:priority:" + uuid.NewString()
	keys := []string{prefix + ":pending", prefix + ":processing"}
	t.Cleanup(func() { client.Del(ctx, keys...) })
	novel := uuid.NewString()
	msg := func(n int) string {
		b, _ := json.Marshal(QueueMessage{NovelID: novel, ChapterIndex: n})
		return string(b)
	}
	one, two, three := msg(1), msg(2), msg(3)
	priorityBytes, _ := json.Marshal(QueueMessage{NovelID: novel, ChapterIndex: 2, Priority: true})
	priorityTwo := string(priorityBytes)
	// Existing FIFO order is 1,2,3; a different novel's pointer must survive too.
	other := `{"novel_id":"other","chapter_index":2}`
	if err := client.LPush(ctx, keys[0], one, two, three, other).Err(); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		result, err := prioritizeScript.Run(ctx, client, keys, novel, 2, priorityTwo).Int()
		if err != nil || result != 1 {
			t.Fatalf("prioritize: %d %v", result, err)
		}
	}
	got, err := client.LRange(ctx, keys[0], 0, -1).Result()
	if err != nil || !reflect.DeepEqual(got, []string{other, three, one, priorityTwo}) {
		t.Fatalf("queue: %v %v", got, err)
	}
	if err := client.LMove(ctx, keys[0], keys[1], "RIGHT", "LEFT").Err(); err != nil {
		t.Fatal(err)
	}
	result, err := prioritizeScript.Run(ctx, client, keys, novel, 2, two).Int()
	if err != nil || result != 0 {
		t.Fatalf("active chapter duplicated: %d %v", result, err)
	}
	got, _ = client.LRange(ctx, keys[0], 0, -1).Result()
	if !reflect.DeepEqual(got, []string{other, three, one}) {
		t.Fatalf("pending changed: %v", got)
	}
}

func TestPriorityRetriesErroredChapterWithoutClearingQueue(t *testing.T) {
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
	insertTestChapter(t, store, novel, 1, "priority-one")
	insertTestChapter(t, store, novel, 2, "priority-two")
	if _, err := store.db.Exec(ctx, `UPDATE chapter SET status='error' WHERE novel_id=$1 AND chapter_index=2`, novel); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, key := range []string{pendingQueue, "jobs:processing"} {
			rows, _ := store.redis.LRange(ctx, key, 0, -1).Result()
			for _, raw := range rows {
				var m QueueMessage
				if json.Unmarshal([]byte(raw), &m) == nil && m.NovelID == novel {
					store.redis.LRem(ctx, key, 0, raw)
				}
			}
		}
	})
	if _, err := store.queueTranslationRange(ctx, novel, 1, 1); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if moved, err := store.prioritizeChapter(ctx, novel, 2); err != nil || !moved {
			t.Fatalf("retry: %v %v", moved, err)
		}
	}
	raw, err := store.redis.LIndex(ctx, pendingQueue, -1).Result()
	var m QueueMessage
	if err != nil || json.Unmarshal([]byte(raw), &m) != nil || m.ChapterIndex != 2 || m.NovelID != novel || !m.Priority {
		t.Fatalf("priority pointer %q %v", raw, err)
	}
	var status string
	if err := store.db.QueryRow(ctx, `SELECT status FROM chapter WHERE novel_id=$1 AND chapter_index=2`, novel).Scan(&status); err != nil || status != "queued" {
		t.Fatalf("retry status %s %v", status, err)
	}
}
