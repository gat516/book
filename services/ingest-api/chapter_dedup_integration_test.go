package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"testing"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
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
		store.db.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
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

// Intercept Redis before network I/O: these tests must never put fixture chapters
// onto the real worker's queue.
type enqueueRecorder struct {
	indices []int
	failAt  int
}

func (h *enqueueRecorder) DialHook(next redis.DialHook) redis.DialHook { return next }
func (h *enqueueRecorder) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}
func (h *enqueueRecorder) ProcessHook(_ redis.ProcessHook) redis.ProcessHook {
	return func(_ context.Context, cmd redis.Cmder) error {
		if cmd.Name() != "lpush" {
			return fmt.Errorf("unexpected Redis command %s", cmd.Name())
		}
		var msg QueueMessage
		if err := json.Unmarshal(cmd.Args()[2].([]byte), &msg); err != nil {
			return err
		}
		if h.failAt > 0 && len(h.indices)+1 == h.failAt {
			return errors.New("injected enqueue failure")
		}
		h.indices = append(h.indices, msg.ChapterIndex)
		return nil
	}
}

func TestTranslationRangeQueuesInOrderAndReleasesUnsentChapters(t *testing.T) {
	for _, failAt := range []int{0, 1, 2} {
		t.Run(fmt.Sprintf("fail_at_%d", failAt), func(t *testing.T) {
			store := integrationStore(t)
			novelID := seedNovelForChapters(t, store)
			for _, index := range []int{3, 1, 2} {
				insertTestChapter(t, store, novelID, index, fmt.Sprintf("sha256:queue-%d", index))
			}
			recorder := &enqueueRecorder{failAt: failAt}
			store.redis = redis.NewClient(&redis.Options{Addr: "unused:0"})
			store.redis.AddHook(recorder)
			t.Cleanup(func() { store.redis.Close() })
			queued, err := store.queueTranslationRange(context.Background(), novelID, 1, 3)
			if failAt == 0 {
				if err != nil || !reflect.DeepEqual(queued, []int{1, 2, 3}) ||
					!reflect.DeepEqual(recorder.indices, []int{1, 2, 3}) {
					t.Fatalf("queued=%v pushed=%v err=%v", queued, recorder.indices, err)
				}
			} else if err == nil {
				t.Fatal("expected enqueue failure")
			}
			for index := 1; index <= 3; index++ {
				want := "queued"
				if failAt > 0 && index >= failAt {
					want = "ingested"
				}
				var status string
				if err := store.db.QueryRow(context.Background(),
					`SELECT status FROM chapter WHERE novel_id = $1 AND chapter_index = $2`,
					novelID, index).Scan(&status); err != nil || status != want {
					t.Fatalf("chapter %d status=%q want=%q err=%v", index, status, want, err)
				}
			}
			// Repeating an already successful request must not enqueue duplicates.
			if failAt == 0 {
				again, err := store.queueTranslationRange(context.Background(), novelID, 1, 3)
				if err != nil || len(again) != 0 || len(recorder.indices) != 3 {
					t.Fatalf("repeat queued=%v pushed=%v err=%v", again, recorder.indices, err)
				}
			} else {
				recorder.failAt = 0
				again, err := store.queueTranslationRange(context.Background(), novelID, 1, 3)
				if err != nil || len(again) != 4-failAt ||
					!reflect.DeepEqual(recorder.indices, []int{1, 2, 3}) {
					t.Fatalf("retry queued=%v pushed=%v err=%v", again, recorder.indices, err)
				}
			}
		})
	}
}
