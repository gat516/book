package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/redis/go-redis/v9"
)

// A records rebuild must enqueue enrichment pointers. A plain queue pointer for a
// saved chapter is stale by definition: the worker is allowed to discard it once the
// chapter's translation is already done.
func TestRebuildRecordsQueuesEnrichmentAndCreatesValidGeneration(t *testing.T) {
	store := integrationStore(t)
	novelID := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelID, 1, "records-rebuild-1")
	insertTestChapter(t, store, novelID, 2, "records-rebuild-2")

	recorder := &recordsEnqueueRecorder{}
	store.redis = redis.NewClient(&redis.Options{Addr: "unused:0"})
	store.redis.AddHook(recorder)
	t.Cleanup(func() { _ = store.redis.Close() })

	generation, count, err := store.rebuildRecords(context.Background(), novelID)
	if err != nil {
		t.Fatalf("rebuild: %v", err)
	}
	if count != 2 || len(recorder.messages) != 2 {
		t.Fatalf("count=%d queued=%d messages=%+v", count, len(recorder.messages), recorder.messages)
	}
	for _, msg := range recorder.messages {
		if !msg.Enrichment {
			t.Errorf("chapter %d was queued without enrichment flag", msg.ChapterIndex)
		}
	}
	var state, prompt, checks, sourceLang, targetLang string
	if err := store.db.QueryRow(context.Background(),
		`SELECT state,prompt_version,checks_version,source_lang,target_lang
		   FROM record_generation WHERE id=$1`, generation,
	).Scan(&state, &prompt, &checks, &sourceLang, &targetLang); err != nil {
		t.Fatalf("generation row: %v", err)
	}
	if state != "active" || prompt == "" || checks == "" || sourceLang != "zh" || targetLang != "en" {
		t.Fatalf("invalid generation metadata: state=%q prompt=%q checks=%q langs=%q/%q", state, prompt, checks, sourceLang, targetLang)
	}
	rebuildStatus, err := store.recordsRebuildStatus(context.Background(), novelID)
	if err != nil {
		t.Fatalf("rebuild status: %v", err)
	}
	if rebuildStatus.ActiveGenerationID == nil || *rebuildStatus.ActiveGenerationID != generation ||
		rebuildStatus.PredecessorGenerationID == nil || !rebuildStatus.Discardable {
		t.Fatalf("unexpected rebuild status: %+v", rebuildStatus)
	}
	if _, _, err := store.rebuildRecords(context.Background(), novelID); !errors.Is(err, ErrRecordsRebuildActive) {
		t.Fatalf("empty eligible replacement was not fenced: %v", err)
	}
	for _, msg := range recorder.messages {
		if msg.RecordGenerationID != generation {
			t.Errorf("chapter %d queued for generation %q, want %q", msg.ChapterIndex, msg.RecordGenerationID, generation)
		}
	}
}

func TestDiscardMidRebuildRestoresPredecessorAndSequentialRebuildsRemainPossible(t *testing.T) {
	store := integrationStore(t)
	novelID := seedNovelForChapters(t, store)
	insertTestChapter(t, store, novelID, 1, "discard-1")
	insertTestChapter(t, store, novelID, 2, "discard-2")
	if _, err := store.db.Exec(context.Background(),
		`UPDATE chapter SET status='done',translation_ready=true WHERE novel_id=$1`, novelID); err != nil {
		t.Fatalf("make chapters readable: %v", err)
	}
	recorder := &recordsEnqueueRecorder{}
	store.redis = redis.NewClient(&redis.Options{Addr: "unused:0"})
	store.redis.AddHook(recorder)
	t.Cleanup(func() { _ = store.redis.Close() })

	first, _, err := store.rebuildRecords(context.Background(), novelID)
	if err != nil {
		t.Fatalf("first rebuild: %v", err)
	}
	// One published chapter is enough to prove discard preserves immutable partial work;
	// chapter two remains unfinished, so the replacement is still safely discardable.
	if _, err := store.db.Exec(context.Background(), `INSERT INTO record_run
		(novel_id,generation_id,chapter_index,source_hash,request_identity,status)
		VALUES ($1,$2,1,'discard-1','test','published')`, novelID, first); err != nil {
		t.Fatalf("seed partial published run: %v", err)
	}
	if err := store.discardRecordsRebuild(context.Background(), novelID, first); err != nil {
		t.Fatalf("discard partial rebuild: %v", err)
	}
	var active, state string
	if err := store.db.QueryRow(context.Background(),
		`SELECT n.active_record_generation::text,g.state FROM novel n JOIN record_generation g ON g.id=$1 WHERE n.id=$2`, first, novelID).
		Scan(&active, &state); err != nil {
		t.Fatalf("read discarded generation: %v", err)
	}
	if active == first || state != "retired" {
		t.Fatalf("discard did not retire replacement: active=%s state=%s", active, state)
	}

	second, _, err := store.rebuildRecords(context.Background(), novelID)
	if err != nil {
		t.Fatalf("sequential rebuild: %v", err)
	}
	if second == first {
		t.Fatal("sequential rebuild reused discarded generation")
	}
	if _, err := store.db.Exec(context.Background(), `INSERT INTO record_run
		(novel_id,generation_id,chapter_index,source_hash,request_identity,status)
		VALUES ($1,$2,1,'discard-1','test','published'),
		       ($1,$2,2,'discard-2','test','published')`, novelID, second); err != nil {
		t.Fatalf("seed completed replacement: %v", err)
	}
	if _, _, err := store.rebuildRecords(context.Background(), novelID); err != nil {
		t.Fatalf("rebuild after completed replacement: %v", err)
	}
}

type recordsEnqueueRecorder struct {
	messages []QueueMessage
	payloads []string
}

func (h *recordsEnqueueRecorder) DialHook(next redis.DialHook) redis.DialHook { return next }

func (h *recordsEnqueueRecorder) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}

func (h *recordsEnqueueRecorder) ProcessHook(_ redis.ProcessHook) redis.ProcessHook {
	return func(_ context.Context, cmd redis.Cmder) error {
		// Status reads the queue to report whether graph work is running; answer from what
		// this recorder was sent, newest first like a real LPUSH list.
		if cmd.Name() == "lrange" {
			values := []string{}
			if cmd.Args()[1] == pendingQueue {
				for i := len(h.payloads) - 1; i >= 0; i-- {
					values = append(values, h.payloads[i])
				}
			}
			cmd.(*redis.StringSliceCmd).SetVal(values)
			return nil
		}
		if cmd.Name() != "lpush" {
			return fmt.Errorf("unexpected Redis command %s", cmd.Name())
		}
		args := cmd.Args()
		payload, ok := args[2].([]byte)
		if !ok {
			return fmt.Errorf("queue payload has type %T", args[2])
		}
		var msg QueueMessage
		if err := json.Unmarshal(payload, &msg); err != nil {
			return err
		}
		h.messages = append(h.messages, msg)
		h.payloads = append(h.payloads, string(payload))
		return nil
	}
}
