package main

import (
	"context"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

func TestQueueControlValidation(t *testing.T) {
	badMode, badID, validMode, validID := "unknown", "not-a-uuid", "focused", uuid.NewString()
	for _, p := range []queueControlPatch{{}, {Mode: &badMode}, {FocusNovelID: &badID}} {
		if err := p.validate(); err == nil {
			t.Fatal("invalid settings accepted")
		}
	}
	p := queueControlPatch{Mode: &validMode, FocusNovelID: &validID}
	if err := p.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestQueueFocusPreservesPauseAndDeletionPreservesOtherFocus(t *testing.T) {
	url := os.Getenv("INGEST_TEST_REDIS_URL")
	if url == "" {
		t.Skip("INGEST_TEST_REDIS_URL is not set")
	}
	opts, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(opts)
	defer client.Close()
	ctx := context.Background()
	key := "test:queue-control:" + uuid.NewString()
	pending := key + ":pending"
	defer client.Del(ctx, key, pending)
	store := &Store{redis: client}
	mode, first, second := "paused", uuid.NewString(), uuid.NewString()
	for _, patch := range []queueControlPatch{{Mode: &mode}, {FocusNovelID: &first}, {FocusNovelID: &second}} {
		if err := store.applyQueueControl(ctx, key, patch); err != nil {
			t.Fatal(err)
		}
	}
	settings, err := client.HGetAll(ctx, key).Result()
	if err != nil || settings["mode"] != "paused" || settings["focus_novel_id"] != second {
		t.Fatalf("settings: %v %v", settings, err)
	}
	if err := purgePendingScript.Run(ctx, client, []string{pending, key}, first).Err(); err != nil {
		t.Fatal(err)
	}
	if focus := client.HGet(ctx, key, "focus_novel_id").Val(); focus != second {
		t.Fatal("deletion erased newer focus")
	}
	if err := purgePendingScript.Run(ctx, client, []string{pending, key}, second).Err(); err != nil {
		t.Fatal(err)
	}
	if client.HExists(ctx, key, "focus_novel_id").Val() || client.HGet(ctx, key, "mode").Val() != "paused" {
		t.Fatal("deleted focus retained or pause lost")
	}
}
