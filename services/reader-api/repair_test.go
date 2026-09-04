package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const testOperatorToken = "operator-secret"

func repairRequest(t *testing.T, api *API, target, operator string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, strings.NewReader(""))
	req.Header.Set("X-Reader-ID", "reader-a")
	if operator != "" {
		req.Header.Set("X-Operator-Token", operator)
	}
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
		repairAbandoned,
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
	response := repairRequest(t, &API{store: store}, "/novels/"+testNovelID+"/repair", "")
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

func TestRepairStatusReportsOperatorFlag(t *testing.T) {
	tests := []struct {
		name     string
		token    string
		header   string
		operator bool
	}{
		{name: "no token configured", token: "", header: testOperatorToken, operator: false},
		{name: "no header presented", token: testOperatorToken, header: "", operator: false},
		{name: "wrong token", token: testOperatorToken, header: "guess", operator: false},
		{name: "correct token", token: testOperatorToken, header: testOperatorToken, operator: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			api := &API{store: readyFake(), operatorToken: test.token}
			response := repairRequest(t, api, "/novels/"+testNovelID+"/repair", test.header)
			if response.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200", response.Code)
			}
			var status RepairStatus
			if err := json.Unmarshal(response.Body.Bytes(), &status); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if status.Operator != test.operator {
				t.Errorf("operator = %v, want %v", status.Operator, test.operator)
			}
			if status.NovelID != testNovelID {
				t.Errorf("novel_id = %q, want %q", status.NovelID, testNovelID)
			}
		})
	}
}

// requireOperator is what repair WRITES will gate on. It must fail closed when the
// deployment never configured a token, rather than accepting an empty header.
func TestRequireOperatorFailsClosed(t *testing.T) {
	tests := []struct {
		name   string
		token  string
		header string
		want   int
		allow  bool
	}{
		{name: "unconfigured", token: "", header: "", want: http.StatusServiceUnavailable},
		{name: "unconfigured with header", token: "", header: "anything", want: http.StatusServiceUnavailable},
		{name: "missing header", token: testOperatorToken, header: "", want: http.StatusForbidden},
		{name: "wrong token", token: testOperatorToken, header: "guess", want: http.StatusForbidden},
		{name: "correct token", token: testOperatorToken, header: testOperatorToken, allow: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			api := &API{store: readyFake(), operatorToken: test.token}
			req := httptest.NewRequest(http.MethodPost, "/novels/"+testNovelID+"/repair", nil)
			if test.header != "" {
				req.Header.Set("X-Operator-Token", test.header)
			}
			recorder := httptest.NewRecorder()
			if got := api.requireOperator(recorder, req); got != test.allow {
				t.Fatalf("requireOperator = %v, want %v", got, test.allow)
			}
			if test.allow {
				return
			}
			if recorder.Code != test.want {
				t.Errorf("status = %d, want %d", recorder.Code, test.want)
			}
		})
	}
}

func TestRepairStatusRejectsInvalidNovelID(t *testing.T) {
	response := repairRequest(t, &API{store: readyFake()}, "/novels/not-a-uuid/repair", "")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", response.Code)
	}
}

// Repair writes are the whole reason the operator boundary exists. Every one of them must
// refuse before it reaches ingest-api, which holds the internal token.
func TestRepairWritesRequireOperator(t *testing.T) {
	targets := []struct{ method, path string }{
		{http.MethodPost, "/novels/" + testNovelID + "/repair"},
		{http.MethodDelete, "/novels/" + testNovelID + "/repair/" + testEntityID},
		{http.MethodGet, "/novels/" + testNovelID + "/repair/preview"},
	}
	for _, target := range targets {
		t.Run(target.method+" "+target.path, func(t *testing.T) {
			ingest := &fakeIngestClient{response: json.RawMessage(`{}`), status: http.StatusAccepted}
			api := &API{store: readyFake(), ingest: ingest, operatorToken: testOperatorToken}

			req := httptest.NewRequest(target.method, target.path, strings.NewReader(`{"track":"graph","action":"prepare"}`))
			req.Header.Set("X-Reader-ID", "reader-a")
			recorder := httptest.NewRecorder()
			api.routes().ServeHTTP(recorder, req)
			if recorder.Code != http.StatusForbidden {
				t.Fatalf("without operator token: status = %d, want 403", recorder.Code)
			}
			if ingest.lastBody != nil {
				t.Error("request reached ingest-api despite failing authorization")
			}

			// And with no token configured at all, it must fail closed rather than open.
			unconfigured := &API{store: readyFake(), ingest: &fakeIngestClient{}, operatorToken: ""}
			req = httptest.NewRequest(target.method, target.path, strings.NewReader(`{}`))
			req.Header.Set("X-Reader-ID", "reader-a")
			req.Header.Set("X-Operator-Token", testOperatorToken)
			recorder = httptest.NewRecorder()
			unconfigured.routes().ServeHTTP(recorder, req)
			if recorder.Code != http.StatusServiceUnavailable {
				t.Fatalf("unconfigured: status = %d, want 503", recorder.Code)
			}
		})
	}
}

// requested_by is an audit label taken from the request, never from the body — otherwise
// any caller could attribute a quarantine to someone else.
func TestPostRepairOverridesRequestedBy(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"id":"x"}`), status: http.StatusAccepted}
	api := &API{store: readyFake(), ingest: ingest, operatorToken: testOperatorToken}

	req := httptest.NewRequest(http.MethodPost, "/novels/"+testNovelID+"/repair",
		strings.NewReader(`{"track":"graph","action":"prepare","requested_by":"someone-else"}`))
	req.Header.Set("X-Reader-ID", "reader-a")
	req.Header.Set("X-Operator-Token", testOperatorToken)
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)

	if recorder.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", recorder.Code)
	}
	var forwarded map[string]any
	if err := json.Unmarshal(ingest.lastBody, &forwarded); err != nil {
		t.Fatalf("decode forwarded body: %v", err)
	}
	if forwarded["requested_by"] != "reader-a" {
		t.Errorf("requested_by = %v, want the request's reader id", forwarded["requested_by"])
	}
	if forwarded["action"] != "prepare" || forwarded["track"] != "graph" {
		t.Errorf("action/track were not forwarded intact: %v", forwarded)
	}
}

func TestRepairPreviewRejectsUnknownTrack(t *testing.T) {
	api := &API{store: readyFake(), operatorToken: testOperatorToken}
	req := httptest.NewRequest(http.MethodGet, "/novels/"+testNovelID+"/repair/preview?track=nonsense", nil)
	req.Header.Set("X-Reader-ID", "reader-a")
	req.Header.Set("X-Operator-Token", testOperatorToken)
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", recorder.Code)
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

func operatorRequest(token, remoteAddr string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/novels/"+testNovelID+"/repair", nil)
	req.Header.Set("X-Reader-ID", "reader-a")
	if token != "" {
		req.Header.Set("X-Operator-Token", token)
	}
	if remoteAddr != "" {
		req.RemoteAddr = remoteAddr
	}
	return req
}

// The single most important property of the throttle: an ordinary reader polling repair
// status sends no operator header, and must never be counted as a failed attempt. Getting
// this backwards would let normal reader traffic lock the operator out of their own book.
func TestMissingOperatorHeaderIsNeverThrottled(t *testing.T) {
	api := &API{store: readyFake(), operatorToken: testOperatorToken, throttle: newAuthThrottle()}
	for i := 0; i < throttleFreeAttempts*10; i++ {
		recorder := httptest.NewRecorder()
		if api.requireOperator(recorder, operatorRequest("", "203.0.113.5:5555")) {
			t.Fatal("no token should never authorize")
		}
		if recorder.Code != http.StatusForbidden {
			t.Fatalf("attempt %d: status = %d, want 403 (never 429)", i, recorder.Code)
		}
	}
	// And the operator is still free to act immediately.
	recorder := httptest.NewRecorder()
	if !api.requireOperator(recorder, operatorRequest(testOperatorToken, "203.0.113.5:5555")) {
		t.Fatalf("valid token was refused after reader traffic: %d", recorder.Code)
	}
}

func TestWrongOperatorTokensEscalateTo429(t *testing.T) {
	throttle := newAuthThrottle()
	now := time.Now()
	throttle.now = func() time.Time { return now }
	api := &API{store: readyFake(), operatorToken: testOperatorToken, throttle: throttle}

	// The free attempts are refused, but not throttled.
	for i := 0; i < throttleFreeAttempts; i++ {
		recorder := httptest.NewRecorder()
		api.requireOperator(recorder, operatorRequest("guess", "198.51.100.7:4444"))
		if recorder.Code != http.StatusForbidden {
			t.Fatalf("attempt %d: status = %d, want 403", i+1, recorder.Code)
		}
	}
	// The next one starts the backoff.
	recorder := httptest.NewRecorder()
	api.requireOperator(recorder, operatorRequest("guess", "198.51.100.7:4444"))
	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", recorder.Code)
	}
	if recorder.Header().Get("Retry-After") == "" {
		t.Error("429 must carry Retry-After")
	}

	// While blocked, even the correct token waits. That is the point of a lockout, and it
	// is why the block always expires rather than being permanent.
	recorder = httptest.NewRecorder()
	if api.requireOperator(recorder, operatorRequest(testOperatorToken, "198.51.100.7:4444")) {
		t.Error("a blocked caller should not authorize, even with the right token")
	}

	// A different caller is unaffected.
	recorder = httptest.NewRecorder()
	if !api.requireOperator(recorder, operatorRequest(testOperatorToken, "198.51.100.9:4444")) {
		t.Errorf("an unrelated address was throttled: %d", recorder.Code)
	}

	// Once the delay elapses, the operator gets straight back in and the record clears.
	now = now.Add(throttleMaxDelay)
	recorder = httptest.NewRecorder()
	if !api.requireOperator(recorder, operatorRequest(testOperatorToken, "198.51.100.7:4444")) {
		t.Fatalf("valid token still refused after the block expired: %d", recorder.Code)
	}
	if len(throttle.records) != 0 {
		t.Errorf("a successful auth must clear the caller's record, got %d", len(throttle.records))
	}
}

func TestBackoffIsBoundedAndKeyedByHost(t *testing.T) {
	throttle := newAuthThrottle()
	var delay time.Duration
	for i := 0; i < 40; i++ {
		delay = throttle.recordFailure("198.51.100.7")
	}
	if delay != throttleMaxDelay {
		t.Errorf("delay = %s, want it capped at %s (a shift that far must not overflow)", delay, throttleMaxDelay)
	}

	// The port must not be part of the key, or every new connection is a fresh budget.
	if got := clientKey(operatorRequest("x", "198.51.100.7:1111")); got != "198.51.100.7" {
		t.Errorf("clientKey = %q, want the host alone", got)
	}
	if got := clientKey(operatorRequest("x", "not-a-host-port")); got != "not-a-host-port" {
		t.Errorf("clientKey = %q, want the raw value when it has no port", got)
	}
}

// A configured-but-weak token must stop the service, not silently protect nothing.
func TestValidateOperatorToken(t *testing.T) {
	tests := []struct {
		name    string
		token   string
		wantErr bool
	}{
		{name: "unset disables repair and is fine", token: "", wantErr: false},
		{name: "short is a misconfiguration", token: "hunter2", wantErr: true},
		{name: "one short of the bound", token: strings.Repeat("a", minRepairOperatorToken-1), wantErr: true},
		{name: "exactly the bound", token: strings.Repeat("a", minRepairOperatorToken), wantErr: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := validateOperatorToken(test.token)
			if (err != nil) != test.wantErr {
				t.Fatalf("validateOperatorToken(%d chars) error = %v, wantErr %v",
					len(test.token), err, test.wantErr)
			}
			if err != nil && !strings.Contains(err.Error(), "leave it unset") {
				t.Errorf("the error should say how to disable repair instead: %v", err)
			}
		})
	}
}

// A blocked caller reports operator:false even with the right token, so the client needs
// to be able to tell that apart from a rejected token — otherwise it discards a valid one
// because someone else tripped the limiter from the same address.
func TestThrottledOperatorIsDistinguishableFromRejected(t *testing.T) {
	api := &API{store: readyFake(), operatorToken: testOperatorToken, throttle: newAuthThrottle()}
	addr := "198.51.100.30:9999"

	wrong := repairStatusFor(t, api, "nope", addr)
	if wrong.Operator || wrong.OperatorThrottled {
		t.Fatalf("a merely wrong token: operator=%v throttled=%v, want false/false",
			wrong.Operator, wrong.OperatorThrottled)
	}
	for i := 0; i < throttleFreeAttempts; i++ {
		repairStatusFor(t, api, "nope", addr)
	}
	blocked := repairStatusFor(t, api, testOperatorToken, addr)
	if blocked.Operator {
		t.Error("a blocked caller must not be treated as an operator")
	}
	if !blocked.OperatorThrottled {
		t.Error("a blocked caller must be reported as throttled, not as simply rejected")
	}
}

func repairStatusFor(t *testing.T, api *API, token, addr string) RepairStatus {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/novels/"+testNovelID+"/repair", nil)
	req.Header.Set("X-Reader-ID", "reader-a")
	req.Header.Set("X-Operator-Token", token)
	req.RemoteAddr = addr
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", recorder.Code)
	}
	var status RepairStatus
	if err := json.Unmarshal(recorder.Body.Bytes(), &status); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return status
}
