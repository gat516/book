package main

import (
	"context"
	"encoding/json"
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
}

type recordsEnqueueRecorder struct {
	messages []QueueMessage
}

func (h *recordsEnqueueRecorder) DialHook(next redis.DialHook) redis.DialHook { return next }

func (h *recordsEnqueueRecorder) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}

func (h *recordsEnqueueRecorder) ProcessHook(_ redis.ProcessHook) redis.ProcessHook {
	return func(_ context.Context, cmd redis.Cmder) error {
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
		return nil
	}
}
