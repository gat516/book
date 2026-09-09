package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestChapterKnowledgeBlockedDetailUsesSharedSafeRepairVocabulary(t *testing.T) {
	for _, category := range []string{
		repairCredentialErr, repairCredentialRej, repairRateLimited,
		repairQuotaExhausted, repairModelUnavailable, repairModelServerErr,
		repairUnreachable,
	} {
		detail := chapterKnowledgeBlockedDetail(category)
		if detail == "" {
			t.Fatalf("category %q has no detail", category)
		}
		if strings.Contains(strings.ToLower(detail), "local ollama") {
			t.Fatalf("category %q has provider-specific detail: %q", category, detail)
		}
	}
	if got := chapterKnowledgeBlockedDetail("not-a-real-category"); got != repairFailureDetail[repairUnknownCause] {
		t.Fatalf("unknown category detail = %q, want unknown detail", got)
	}
}

func TestChapterKnowledgeRunViewJSONExposesCategoryAndDetailWithoutError(t *testing.T) {
	when := time.Date(2026, time.September, 9, 12, 0, 0, 0, time.UTC)
	view := ChapterKnowledgeRunView{
		ID: "run", Mode: "ordinary", Scope: "facts", State: "failed", CreatedAt: when,
		BlockedCategory: repairCredentialRej,
		BlockedDetail:   chapterKnowledgeBlockedDetail(repairCredentialRej), BlockedAt: &when,
	}
	body, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	encoded := string(body)
	if !strings.Contains(encoded, `"blocked_category":"credential_rejected"`) ||
		!strings.Contains(encoded, `"blocked_detail":"`) ||
		!strings.Contains(encoded, `"blocked_at":"2026-09-09T12:00:00Z"`) {
		t.Fatalf("blocked fields missing from JSON: %s", encoded)
	}
	if strings.Contains(encoded, "error") {
		t.Fatalf("raw error field leaked into JSON: %s", encoded)
	}
}
