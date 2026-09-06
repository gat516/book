package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func repairRequest(t *testing.T, api *API, target string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, strings.NewReader(""))
	req.Header.Set("X-Reader-ID", "reader-a")
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	return recorder
}

// Every category must have a sentence, or the API silently returns an empty detail.
//
// Classification itself moved to pipeline/failures.py (migration 0046) so that the raw
// exception text never reaches a read path — its behaviour is covered by
// test_failure_category_classifies_repair_action_errors there. What is left for Go is
// rendering, so this asserts the rendering is total, and tests/test_repair.py's
// test_category_vocabulary_matches_reader_api asserts the two sides agree on the words.
func TestEveryFailureCategoryHasDetail(t *testing.T) {
	categories := []string{
		repairModelChanged, repairInputChanged, repairPromptTooBig, repairServingDrift,
		repairFenced, repairTimeout, repairUnreachable, repairUnknownCause, repairNotRebuildErr,
		repairTruncated, repairReviewRejected, repairNotFound, repairCancelled,
		repairAbandoned, repairModelMissing, repairCredentialErr,
	}
	for _, category := range categories {
		if strings.TrimSpace(repairFailureDetail[category]) == "" {
			t.Errorf("category %q has no reader-facing detail", category)
		}
	}
}

func TestBuildTrackStates(t *testing.T) {
	active := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	staging := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

	tests := []struct {
		name           string
		row            repairRow
		wantState      string
		wantWithheld   int
		wantReplacment bool
	}{
		{
			name:      "trusted active revision is ready",
			row:       repairRow{activeRevision: &active, activeTrusted: true, withheldClaims: 12},
			wantState: repairReady,
			// Nothing is withheld while the revision is trusted, whatever the count says.
			wantWithheld: 0,
		},
		{
			name:         "untrusted with no replacement is quarantined",
			row:          repairRow{activeRevision: &active, withheldClaims: 12},
			wantState:    repairQuarantined,
			wantWithheld: 12,
		},
		{
			name: "replacement with chapters left is rebuilding",
			row: repairRow{activeRevision: &active, withheldClaims: 12, replacementID: &staging,
				total: 10, done: 6},
			wantState: repairRebuilding, wantWithheld: 12, wantReplacment: true,
		},
		{
			name: "all chapters done is awaiting review",
			row: repairRow{activeRevision: &active, withheldClaims: 12, replacementID: &staging,
				total: 10, done: 10},
			wantState: repairAwaitingReview, wantWithheld: 12, wantReplacment: true,
		},
		{
			name: "failures with a retry pending still counts as rebuilding",
			row: repairRow{activeRevision: &active, withheldClaims: 12, replacementID: &staging,
				total: 10, done: 7, failed: 3, retryable: true},
			wantState: repairRebuilding, wantWithheld: 12, wantReplacment: true,
		},
		{
			name: "failures with no retries left is failed",
			row: repairRow{activeRevision: &active, withheldClaims: 12, replacementID: &staging,
				total: 10, done: 7, failed: 3},
			wantState: repairFailed, wantWithheld: 12, wantReplacment: true,
		},
		{
			name: "trusted active revision still reports its own failing chapters",
			row: repairRow{activeRevision: &active, activeTrusted: true,
				total: 21, done: 18, failed: 3, retryable: true},
			wantState: repairReady,
		},
		{
			name: "reviewed but awaiting a fresh report is still awaiting_review",
			row: repairRow{activeRevision: &active, withheldClaims: 12, replacementID: &staging,
				total: 10, done: 10, reviewed: true},
			wantState: repairAwaitingReview, wantWithheld: 12, wantReplacment: true,
		},
		{
			name:      "no revision at all is unavailable",
			row:       repairRow{},
			wantState: repairUnavailable,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			track := buildTrack(test.row, nil, nil, "facts")
			if track.State != test.wantState {
				t.Errorf("state = %q, want %q", track.State, test.wantState)
			}
			if track.WithheldClaims != test.wantWithheld {
				t.Errorf("withheld = %d, want %d", track.WithheldClaims, test.wantWithheld)
			}
			if track.Chapters.Total != test.row.total || track.Chapters.Done != test.row.done ||
				track.Chapters.Failed != test.row.failed {
				t.Errorf("chapters = %+v, want total=%d done=%d failed=%d",
					track.Chapters, test.row.total, test.row.done, test.row.failed)
			}
			if (track.Replacement != nil) != test.wantReplacment {
				t.Errorf("replacement present = %v, want %v", track.Replacement != nil, test.wantReplacment)
			}
			if strings.TrimSpace(track.Reason) == "" {
				t.Error("every state must carry a reason sentence")
			}
			if track.Failures == nil {
				t.Error("failures must serialise as [] rather than null")
			}
			if track.RollbackTargets == nil {
				t.Error("rollback_targets must serialise as [] rather than null")
			}
		})
	}
}

// The raw exception text is the one thing that must never cross the wire.
func TestRepairStatusNeverLeaksRawFailureText(t *testing.T) {
	store := readyFake()
	store.repair = RepairStatus{
		Graph: RepairTrack{
			State:  repairFailed,
			Reason: "the replacement stopped",
			Failures: []RepairFailure{{
				ChapterIndex: 4,
				Attempts:     4,
				Category:     repairUnknownCause,
				Detail:       repairFailureDetail[repairUnknownCause],
				OccurredAt:   time.Now(),
			}},
		},
	}
	response := repairRequest(t, &API{store: store}, "/novels/"+testNovelID+"/repair")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	body := response.Body.String()
	for _, forbidden := range []string{"Traceback", "postgres://", "ValueError:"} {
		if strings.Contains(body, forbidden) {
			t.Errorf("response leaked %q: %s", forbidden, body)
		}
	}
	if !strings.Contains(body, `"category":"unknown"`) {
		t.Errorf("expected a safe category in the response: %s", body)
	}
}

func TestRepairStatusRejectsInvalidNovelID(t *testing.T) {
	response := repairRequest(t, &API{store: readyFake()}, "/novels/not-a-uuid/repair")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", response.Code)
	}
}

func TestRepairPreviewRejectsUnknownTrack(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	api := &API{store: readyFake(), operatorToken: token}
	req := httptest.NewRequest(http.MethodGet, "/novels/"+testNovelID+"/repair/preview?track=nonsense", nil)
	req.Header.Set("X-Reader-ID", "reader-a")
	req.Header.Set("X-Operator-Token", token)
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", recorder.Code)
	}
}

func TestRepairStoryAndMutationEndpointsRequireOperator(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	api := &API{store: readyFake(), ingest: &fakeIngestClient{status: http.StatusAccepted}, operatorToken: token}
	for _, test := range []struct {
		method string
		target string
		body   string
	}{
		{http.MethodGet, "/novels/" + testNovelID + "/repair/preview", ""},
		{http.MethodGet, "/novels/" + testNovelID + "/repair/progress", ""},
		{http.MethodPost, "/novels/" + testNovelID + "/repair", `{}`},
	} {
		req := httptest.NewRequest(test.method, test.target, strings.NewReader(test.body))
		req.Header.Set("X-Reader-ID", "reader-a")
		recorder := httptest.NewRecorder()
		api.routes().ServeHTTP(recorder, req)
		if recorder.Code != http.StatusForbidden {
			t.Errorf("%s %s status=%d, want 403", test.method, test.target, recorder.Code)
		}
	}
}

func TestRepairStatusReportsServerVerifiedOperator(t *testing.T) {
	const token = "0123456789abcdef0123456789abcdef"
	api := &API{store: readyFake(), operatorToken: token}
	req := httptest.NewRequest(http.MethodGet, "/novels/"+testNovelID+"/repair", nil)
	req.Header.Set("X-Reader-ID", "reader-a")
	req.Header.Set("X-Operator-Token", token)
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), `"operator":true`) {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestRepairOperatorTokenValidation(t *testing.T) {
	if validateOperatorToken("") != nil {
		t.Fatal("empty token should disable repairs without preventing startup")
	}
	if validateOperatorToken("too-short") == nil {
		t.Fatal("weak configured token must be rejected")
	}
}

// switch() refuses anything but an archived revision, so the panel must never be able to
// offer the active one. Guarding the contract here because the earlier version of the
// panel passed active_revision and every rollback failed at the database.
func TestRollbackTargetsAreSeparateFromTheActiveRevision(t *testing.T) {
	active := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	archived := "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
	track := buildTrack(
		repairRow{activeRevision: &active, activeTrusted: true},
		nil,
		[]RepairRollbackTarget{{RevisionID: archived, Trusted: true, CreatedAt: time.Now()}},
		"facts",
	)
	if len(track.RollbackTargets) != 1 || track.RollbackTargets[0].RevisionID != archived {
		t.Fatalf("rollback targets = %+v", track.RollbackTargets)
	}
	for _, target := range track.RollbackTargets {
		if target.RevisionID == track.ActiveRevision {
			t.Error("the active revision must never be offered as a rollback target")
		}
	}
}

// The reviewed flag exists to separate two states that record_review makes look identical
// (it stores metrics and clears `review` in the same step).
func TestReviewedWithoutHashExplainsTheWait(t *testing.T) {
	active := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	staging := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	hash := "abc123"

	waiting := buildTrack(repairRow{activeRevision: &active, replacementID: &staging,
		total: 4, done: 4, reviewed: true}, nil, nil, "facts")
	ready := buildTrack(repairRow{activeRevision: &active, replacementID: &staging,
		total: 4, done: 4, reviewed: true, reviewHash: &hash}, nil, nil, "facts")
	unreviewed := buildTrack(repairRow{activeRevision: &active, replacementID: &staging,
		total: 4, done: 4}, nil, nil, "facts")

	if waiting.Reason == ready.Reason || waiting.Reason == unreviewed.Reason {
		t.Error("reviewed-awaiting-report must read differently from reviewed and from unreviewed")
	}
	if !waiting.Replacement.Reviewed || waiting.Replacement.ReviewHash != "" {
		t.Errorf("waiting replacement = %+v", waiting.Replacement)
	}
	if ready.Replacement.ReviewHash != hash {
		t.Errorf("ready replacement hash = %q", ready.Replacement.ReviewHash)
	}
}

// A stalled rebuild must not read like a healthy one. This is the case that sent someone
// to journalctl: a dead model endpoint showed "Rebuilding: 0 of 26 done" with an empty
// failure ledger, because the failure happened in resume()'s preamble and was recorded
// nowhere the panel could see.
func TestBlockedRebuildReadsDifferentlyFromASlowOne(t *testing.T) {
	active := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	staging := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	cause := repairUnreachable
	since := time.Now().Add(-5 * time.Minute)

	slow := buildTrack(repairRow{activeRevision: &active, replacementID: &staging,
		total: 26, done: 0, claims: 4, calls: 9}, nil, nil, "facts")
	stalled := buildTrack(repairRow{activeRevision: &active, replacementID: &staging,
		total: 26, done: 0, blockedCat: &cause, blockedAt: &since}, nil, nil, "facts")

	if slow.Blocked != nil {
		t.Error("a healthy rebuild must not report a blockage")
	}
	if stalled.Blocked == nil {
		t.Fatal("a blocked rebuild must report why")
	}
	if stalled.Blocked.Category != repairUnreachable || stalled.Blocked.Detail == "" {
		t.Errorf("blocked = %+v, want a category and a sentence", stalled.Blocked)
	}
	if stalled.Blocked.Since.IsZero() {
		t.Error("blocked must carry since, so the reader can see how long it has been stuck")
	}
	if stalled.Reason == slow.Reason {
		t.Fatal("a stalled rebuild must not read identically to a slow one")
	}
	if !strings.Contains(stalled.Reason, "Stalled") {
		t.Errorf("stalled reason should lead with the blockage: %q", stalled.Reason)
	}
	// And a healthy one should show that it is finding things between chapter boundaries.
	// Model calls, not claims: claims only land per chapter, so they would move exactly
	// when the chapter counter does and say nothing about the chapter in flight.
	if !strings.Contains(slow.Reason, "9 model calls") {
		t.Errorf("a slow rebuild should report completed model calls: %q", slow.Reason)
	}
	if slow.Published.Calls != 9 {
		t.Errorf("published calls = %d, want 9", slow.Published.Calls)
	}
	if slow.Published.Claims != 4 {
		t.Errorf("published claims = %d, want 4", slow.Published.Claims)
	}
}
