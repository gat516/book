package main

// Knowledge repair status — the read half of making a quarantined graph a visible
// product state rather than an operator-only diagnosis.
//
// Background: graph_rebuild.prepare sets graph_revision.trusted=false. From that moment
// reader_graph_revision() returns NULL and every RESTRICTIVE policy bound to it returns
// zero rows, so facts, entities, edges, events and evidence disappear from every reader
// query and from Ask AI while the prose stays readable. That is correct behaviour and it
// is also, today, almost silent: the reader gets one sentence in ReaderPane and no way to
// find out whether anything is being done about it.
//
// This file answers three questions for one novel: are facts being withheld, is a
// replacement being built and how far has it got, and — for the case where a rebuild
// keeps failing — what failed and will it be tried again.
//
// It is a pure read. Nothing here starts, reviews or activates a rebuild; those verbs
// still belong to pipeline.graph_rebuild and pipeline.event_rebuild.

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// RepairStatus is the whole picture for one novel. The two tracks are independent by
// design (see 0039's header): event extraction has its own activation pointer, and
// preparing an event revision never quarantines the entity graph.
type RepairStatus struct {
	NovelID string `json:"novel_id"`
	// Operator reports whether THIS caller presented a valid operator token. The status
	// itself is visible to everyone; the controls that act on it are not.
	Operator bool `json:"operator"`
	// OperatorThrottled separates "your token is wrong" from "you are currently blocked",
	// which Operator alone cannot: a blocked caller reports false even with the right
	// token. Without this the client cannot tell whether to discard a stored token, and
	// would throw away a valid one after someone else tripped the limiter from the same
	// address.
	OperatorThrottled bool        `json:"operator_throttled"`
	Graph             RepairTrack `json:"graph"`
	Events            RepairTrack `json:"events"`
	// Requests are the repair actions asked for through the UI, newest first: what was
	// asked, by whom, and whether the worker has picked it up yet. A click does not act
	// instantly — repair runs on the worker's idle tick, because it must lose to
	// reader-critical translation — so the UI needs somewhere honest to show "pending".
	Requests []RepairRequestView `json:"requests"`
	// History is graph_audit/event_audit: quarantine, review, activate, rollback. It has
	// been recorded since 0023 and never read. It is the answer to "does this book keep
	// getting corrupted", which no single status field can give.
	History []RepairAuditEntry `json:"history"`
}

type RepairRequestView struct {
	ID          string    `json:"id"`
	Track       string    `json:"track"`
	Action      string    `json:"action"`
	State       string    `json:"state"`
	Attempts    int       `json:"attempts"`
	Category    string    `json:"category,omitempty"`
	RequestedBy string    `json:"requested_by"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type RepairAuditEntry struct {
	Track     string    `json:"track"`
	Action    string    `json:"action"`
	CreatedAt time.Time `json:"created_at"`
}

// RepairPreview is the frozen review report for the revision being rebuilt. Operator-only:
// it embeds source quotes from every chapter in the snapshot, ignoring reading progress.
type RepairPreview struct {
	RevisionID string `json:"revision_id"`
	Version    int64  `json:"version"`
	State      string `json:"state"`
	// Report is graph_rebuild.preview's output verbatim. Deliberately opaque here: its
	// shape is defined by the Python that produces it, and re-typing it in Go would be a
	// second definition to keep in sync for no gain.
	Report json.RawMessage `json:"report"`
}

// Repair states. A reader sees the state and the reason; only an operator sees controls.
// paramsRequestLimit bounds a repair body. A review document — 60 mention assessments and
// 30 fact assessments — is a few KiB; ingest-api enforces its own limit on params again.
const paramsRequestLimit = 1 << 20

const (
	// repairReady — the active revision is trusted and readers can see its claims.
	repairReady = "ready"
	// repairQuarantined — the active revision is untrusted and nothing is replacing it.
	// This is where a rollback lands, because rollback deliberately preserves trust
	// (graph_rebuild.switch): returning to a contaminated revision does not restore it.
	repairQuarantined = "quarantined"
	// repairRebuilding — a staging revision exists and still has chapters to process.
	repairRebuilding = "rebuilding"
	// repairAwaitingReview — every chapter is processed; activation now waits on a human.
	repairAwaitingReview = "awaiting_review"
	// repairFailed — chapters failed and no retry is scheduled for any of them.
	repairFailed = "failed"
	// repairUnavailable — this track has no active trusted revision and none staged.
	// Normal for events on a novel that has never had an event revision prepared.
	repairUnavailable = "unavailable"
)

type RepairTrack struct {
	State string `json:"state"`
	// Reason is the server's sentence, so every client says the same thing rather than
	// each inventing its own phrasing from the numbers. Same split as TranslationHealth.
	Reason         string `json:"reason"`
	ActiveRevision string `json:"active_revision,omitempty"`
	ActiveTrusted  bool   `json:"active_trusted"`
	// WithheldClaims counts claims stored on the active revision that readers cannot
	// currently see. Zero when the revision is trusted, because then nothing is withheld.
	WithheldClaims int `json:"withheld_claims"`
	// Chapters describes the run currently doing work: the replacement when one is being
	// built, otherwise the active revision's own enrichment. A trusted graph whose
	// chapters keep failing is the earliest visible form of "this keeps breaking", and it
	// exists before anyone has decided to quarantine anything.
	Chapters    RepairChapters     `json:"chapters"`
	Replacement *RepairReplacement `json:"replacement"`
	// RollbackTargets are the archived revisions rollback may actually target. switch()
	// refuses anything else ("rollback target must be an archived revision"), so offering
	// the active revision — as an earlier version of the panel did — could only ever fail.
	RollbackTargets []RepairRollbackTarget `json:"rollback_targets"`
	// Superseded counts earlier staging revisions this track has accumulated. Restarting
	// a rebuild is the documented recovery from an exhausted one, so these are legitimate
	// — but a large number means this book has been restarted many times.
	Superseded int `json:"superseded"`
	// Failures is bounded (20 per track) and newest first.
	Failures []RepairFailure `json:"failures"`
	// Retryable is false once every failure has exhausted its attempts, which is the
	// point at which waiting stops being a strategy.
	Retryable bool `json:"retryable"`
	// Blocked is why the run cannot proceed AT ALL, as opposed to one chapter failing.
	// Its absence used to be indistinguishable from "working slowly": a dead endpoint
	// left the panel showing 0 done and an empty failure ledger for as long as it lasted.
	Blocked *RepairBlocked `json:"blocked"`
	// Published is what the rebuild has produced so far. The chapter counter only moves
	// once per chapter, and a chapter is many inference calls, so this is what shows a
	// long rebuild is alive between those boundaries.
	Published RepairPublished `json:"published"`
}

type RepairBlocked struct {
	Category string    `json:"category"`
	Detail   string    `json:"detail"`
	Since    time.Time `json:"since"`
}

type RepairPublished struct {
	// Claims and Entities only appear when a WHOLE chapter publishes, so they move at the
	// same moment the chapter counter does. They answer "what has it found".
	Claims   int `json:"claims"`
	Entities int `json:"entities"`
	// Calls is one per completed model call, several per chapter — the only counter that
	// moves inside a chapter, and so the only honest liveness signal. On this hardware a
	// single call can take ten minutes, which is long enough to look hung.
	Calls int `json:"calls"`
}

// RepairProgressFact is one claim the running rebuild has already published. Operator-only:
// unreviewed, and quoted from anywhere in the book (migration 0047).
type RepairProgressFact struct {
	ID        int64  `json:"id"`
	Entity    string `json:"entity"`
	Kind      string `json:"kind"`
	Attribute string `json:"attribute"`
	Value     string `json:"value"`
	Chapter   int    `json:"chapter_index"`
	Quote     string `json:"quote,omitempty"`
}

type RepairChapters struct {
	Total   int `json:"total"`
	Done    int `json:"done"`
	Failed  int `json:"failed"`
	Running int `json:"running"`
}

type RepairReplacement struct {
	RevisionID    string     `json:"revision_id"`
	Model         string     `json:"model,omitempty"`
	PromptVersion string     `json:"prompt_version,omitempty"`
	CreatedAt     *time.Time `json:"created_at,omitempty"`
	// ActivationEligible comes from the frozen preview report, not from this API's own
	// judgement. Nil means no report has been taken yet.
	ActivationEligible *bool  `json:"activation_eligible"`
	ReviewHash         string `json:"review_hash,omitempty"`
	// Reviewed is true once record_review has stored derived metrics. It clears
	// `review` as it does so, so between a review and the worker freezing a fresh report
	// there is a window with Reviewed=true and ReviewHash empty. That window is why this
	// field exists: without it the UI can only make the Activate button disappear.
	Reviewed bool `json:"reviewed"`
}

type RepairRollbackTarget struct {
	RevisionID string `json:"revision_id"`
	// Trusted matters at the point of choosing: rolling back to an untrusted revision
	// does not restore its facts, because switch deliberately preserves trust.
	Trusted   bool      `json:"trusted"`
	CreatedAt time.Time `json:"created_at"`
}

type RepairFailure struct {
	ChapterIndex int    `json:"chapter_index"`
	Attempts     int    `json:"attempts"`
	Category     string `json:"category"`
	// Detail is a fixed, safe sentence chosen by Category — never the stored exception
	// text, which is freeform and can contain source prose or connection strings.
	Detail     string     `json:"detail"`
	RetryAt    *time.Time `json:"retry_at"`
	OccurredAt time.Time  `json:"occurred_at"`
}

// Failure categories. These are RENDERED here, not derived here: pipeline/failures.py
// classifies a failure at the moment the exception is raised and stores only the class, so
// the freeform exception text never reaches a read path (migration 0046). This file's only
// job is to give each class a sentence a reader can act on.
//
// Because Go no longer produces these values, a category Python emits with no entry in the
// map below would render as a blank explanation. tests/test_repair.py enforces the match
// across the language boundary — that guard is now the only thing catching a new class.
const (
	repairModelChanged  = "model_changed"
	repairInputChanged  = "input_changed"
	repairPromptTooBig  = "prompt_too_large"
	repairServingDrift  = "serving_identity_changed"
	repairFenced        = "fenced"
	repairTimeout       = "timeout"
	repairUnreachable   = "model_unreachable"
	repairTruncated     = "output_truncated"
	repairUnknownCause  = "unknown"
	repairNotRebuildErr = "revision_not_rebuildable"
	// These three are only ever produced by pipeline/repair.py, for failures of a repair
	// ACTION rather than of a chapter rebuild. They live in the same vocabulary on
	// purpose: the UI renders one category map, and a category with no sentence would
	// render as a blank explanation.
	repairReviewRejected = "review_rejected"
	repairNotFound       = "not_found"
	repairCancelled      = "cancelled"
	repairAbandoned      = "abandoned"
	repairModelMissing   = "model_not_installed"
)

var repairFailureDetail = map[string]string{
	repairModelChanged:   "the local model or its inference settings changed after this rebuild was snapshotted, so publishing was refused",
	repairInputChanged:   "the saved chapter text changed after this rebuild was snapshotted",
	repairPromptTooBig:   "a chapter produced more context than the local model can be given safely",
	repairServingDrift:   "the model that answered was not the model this rebuild is pinned to",
	repairFenced:         "the rebuild was superseded by a newer revision while this chapter was running",
	repairTimeout:        "the local model did not finish this chapter within the deadline",
	repairTruncated:      "the local model hit its output limit mid-answer; a partial extraction is never published",
	repairUnreachable:    "the local model could not be reached",
	repairNotRebuildErr:  "this revision cannot be rebuilt",
	repairUnknownCause:   "processing failed; the cause was not recognised",
	repairReviewRejected: "the review was rejected: it must approve the current report hash, name a reviewer, and assess every published claim exactly once",
	repairNotFound:       "the book or revision this action referred to no longer exists",
	repairCancelled:      "the request was withdrawn before it started",
	repairAbandoned:      "the worker stopped while running this action and it had no attempts left to retry",
	repairModelMissing:   "the model this rebuild is pinned to is not installed on the configured Ollama endpoint; it cannot be substituted, so start a fresh rebuild pinned to a model that is there",
}

// repairRow is one row of reader_repair_status, before the API decides what it means.
type repairRow struct {
	track          string
	activeRevision *string
	activeTrusted  bool
	activeLegacy   bool
	withheldClaims int
	replacementID  *string
	model          *string
	promptVersion  *string
	createdAt      *time.Time
	total          int
	done           int
	failed         int
	running        int
	eligible       *bool
	reviewHash     *string
	retryable      bool
	reviewed       bool
	superseded     int
	blockedCat     *string
	blockedAt      *time.Time
	claims         int
	entities       int
	calls          int
}

func (s *Store) RepairStatus(ctx context.Context, novelID string) (RepairStatus, error) {
	status := RepairStatus{NovelID: novelID}

	rows, err := s.readerDB.Query(ctx,
		`SELECT track, active_revision::text, active_trusted, active_legacy, withheld_claims,
		        replacement_id::text, replacement_model, replacement_prompt, replacement_created,
		        chapters_total, chapters_done, chapters_failed, chapters_running,
		        activation_eligible, review_hash, retryable, reviewed, superseded,
		        blocked_category, blocked_at, claims_published, entities_created,
		        calls_completed
		   FROM reader_repair_status($1)`, novelID)
	if err != nil {
		return RepairStatus{}, fmt.Errorf("read repair status: %w", err)
	}
	defer rows.Close()

	byTrack := map[string]repairRow{}
	for rows.Next() {
		var row repairRow
		if err := rows.Scan(&row.track, &row.activeRevision, &row.activeTrusted, &row.activeLegacy,
			&row.withheldClaims, &row.replacementID, &row.model, &row.promptVersion, &row.createdAt,
			&row.total, &row.done, &row.failed, &row.running,
			&row.eligible, &row.reviewHash, &row.retryable,
			&row.reviewed, &row.superseded,
			&row.blockedCat, &row.blockedAt, &row.claims, &row.entities,
			&row.calls); err != nil {
			return RepairStatus{}, fmt.Errorf("scan repair status: %w", err)
		}
		byTrack[row.track] = row
	}
	if err := rows.Err(); err != nil {
		return RepairStatus{}, fmt.Errorf("read repair status: %w", err)
	}

	failures, err := s.repairFailures(ctx, novelID)
	if err != nil {
		return RepairStatus{}, err
	}
	targets, err := s.repairRollbackTargets(ctx, novelID)
	if err != nil {
		return RepairStatus{}, err
	}

	status.Graph = buildTrack(byTrack["graph"], failures["graph"], targets["graph"], "facts")
	status.Events = buildTrack(byTrack["events"], failures["events"], targets["events"], "chapter events")

	if status.Requests, err = s.repairRequests(ctx, novelID); err != nil {
		return RepairStatus{}, err
	}
	if status.History, err = s.repairHistory(ctx, novelID); err != nil {
		return RepairStatus{}, err
	}
	return status, nil
}

func (s *Store) repairRollbackTargets(ctx context.Context, novelID string) (map[string][]RepairRollbackTarget, error) {
	rows, err := s.readerDB.Query(ctx,
		`SELECT track, revision_id::text, trusted, created_at
		   FROM reader_repair_rollback_targets($1)`, novelID)
	if err != nil {
		return nil, fmt.Errorf("read rollback targets: %w", err)
	}
	defer rows.Close()
	byTrack := map[string][]RepairRollbackTarget{}
	for rows.Next() {
		var track string
		var target RepairRollbackTarget
		if err := rows.Scan(&track, &target.RevisionID, &target.Trusted, &target.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan rollback target: %w", err)
		}
		byTrack[track] = append(byTrack[track], target)
	}
	return byTrack, rows.Err()
}

func (s *Store) repairRequests(ctx context.Context, novelID string) ([]RepairRequestView, error) {
	rows, err := s.readerDB.Query(ctx,
		`SELECT id::text, track, action, state, attempts, category, requested_by, created_at, updated_at
		   FROM reader_repair_requests($1)`, novelID)
	if err != nil {
		return nil, fmt.Errorf("read repair requests: %w", err)
	}
	defer rows.Close()
	requests := []RepairRequestView{}
	for rows.Next() {
		var request RepairRequestView
		var category *string
		if err := rows.Scan(&request.ID, &request.Track, &request.Action, &request.State,
			&request.Attempts, &category, &request.RequestedBy,
			&request.CreatedAt, &request.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan repair request: %w", err)
		}
		if category != nil {
			request.Category = *category
		}
		requests = append(requests, request)
	}
	return requests, rows.Err()
}

func (s *Store) repairHistory(ctx context.Context, novelID string) ([]RepairAuditEntry, error) {
	rows, err := s.readerDB.Query(ctx,
		`SELECT track, action, created_at FROM reader_repair_history($1)`, novelID)
	if err != nil {
		return nil, fmt.Errorf("read repair history: %w", err)
	}
	defer rows.Close()
	history := []RepairAuditEntry{}
	for rows.Next() {
		var entry RepairAuditEntry
		if err := rows.Scan(&entry.Track, &entry.Action, &entry.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan repair history: %w", err)
		}
		history = append(history, entry)
	}
	return history, rows.Err()
}

// RepairProgress lists what the running rebuild has published so far. Operator-only and
// on operatorDB for the same reason as RepairPreview: unreviewed claims with source
// quotes, ungated by reading progress.
func (s *Store) RepairProgress(ctx context.Context, novelID string) ([]RepairProgressFact, error) {
	rows, err := s.operatorDB.Query(ctx,
		`SELECT fact_id, entity, kind, attribute, value, chapter_index, quote
		   FROM repair_progress($1)`, novelID)
	if err != nil {
		return nil, fmt.Errorf("read repair progress: %w", err)
	}
	defer rows.Close()
	facts := []RepairProgressFact{}
	for rows.Next() {
		var fact RepairProgressFact
		var quote *string
		if err := rows.Scan(&fact.ID, &fact.Entity, &fact.Kind, &fact.Attribute,
			&fact.Value, &fact.Chapter, &quote); err != nil {
			return nil, fmt.Errorf("scan repair progress: %w", err)
		}
		if quote != nil {
			fact.Quote = *quote
		}
		facts = append(facts, fact)
	}
	return facts, rows.Err()
}

// RepairPreview reads the frozen report. It returns story content — source quotes from
// every snapshotted chapter, ignoring reading progress — so it is the ONLY method that uses
// operatorDB, whose role is the only one Postgres will let call repair_preview (0046).
// The operator check in the handler is now defence in depth rather than the whole defence.
func (s *Store) RepairPreview(ctx context.Context, novelID, track string) (RepairPreview, error) {
	var preview RepairPreview
	var report *[]byte
	err := s.operatorDB.QueryRow(ctx,
		`SELECT revision_id::text, version, state, review FROM repair_preview($1,$2)`,
		novelID, track).Scan(&preview.RevisionID, &preview.Version, &preview.State, &report)
	if errors.Is(err, pgx.ErrNoRows) {
		return RepairPreview{}, ErrNotFound
	}
	if err != nil {
		return RepairPreview{}, fmt.Errorf("read repair preview: %w", err)
	}
	if report != nil {
		preview.Report = json.RawMessage(*report)
	}
	return preview, nil
}

func (s *Store) repairFailures(ctx context.Context, novelID string) (map[string][]RepairFailure, error) {
	rows, err := s.readerDB.Query(ctx,
		`SELECT track, chapter_index, attempts, category, retry_at, updated_at
		   FROM reader_repair_failures($1)`, novelID)
	if err != nil {
		return nil, fmt.Errorf("read repair failures: %w", err)
	}
	defer rows.Close()

	byTrack := map[string][]RepairFailure{}
	for rows.Next() {
		var track string
		var failure RepairFailure
		if err := rows.Scan(&track, &failure.ChapterIndex, &failure.Attempts, &failure.Category,
			&failure.RetryAt, &failure.OccurredAt); err != nil {
			return nil, fmt.Errorf("scan repair failure: %w", err)
		}
		failure.Detail = repairFailureDetail[failure.Category]
		if failure.Detail == "" {
			// A class this build does not know about. Say something true rather than
			// nothing; the cross-language test exists to keep this branch unreachable.
			failure.Detail = repairFailureDetail[repairUnknownCause]
		}
		byTrack[track] = append(byTrack[track], failure)
	}
	return byTrack, rows.Err()
}

// buildTrack turns counts into a state and a sentence. noun names what this track's
// claims are called in reader-facing prose ("facts", "chapter events").
func buildTrack(row repairRow, failures []RepairFailure, targets []RepairRollbackTarget, noun string) RepairTrack {
	track := RepairTrack{
		ActiveTrusted:   row.activeTrusted,
		WithheldClaims:  0,
		Chapters:        RepairChapters{Total: row.total, Done: row.done, Failed: row.failed, Running: row.running},
		Failures:        failures,
		Published:       RepairPublished{Claims: row.claims, Entities: row.entities, Calls: row.calls},
		RollbackTargets: targets,
		Superseded:      row.superseded,
		Retryable:       row.retryable,
	}
	if track.Failures == nil {
		track.Failures = []RepairFailure{}
	}
	if track.RollbackTargets == nil {
		track.RollbackTargets = []RepairRollbackTarget{}
	}
	if row.activeRevision != nil {
		track.ActiveRevision = *row.activeRevision
	}
	if row.replacementID != nil {
		replacement := &RepairReplacement{
			RevisionID:         *row.replacementID,
			CreatedAt:          row.createdAt,
			ActivationEligible: row.eligible,
		}
		if row.model != nil {
			replacement.Model = *row.model
		}
		if row.promptVersion != nil {
			replacement.PromptVersion = *row.promptVersion
		}
		if row.reviewHash != nil {
			replacement.ReviewHash = *row.reviewHash
		}
		replacement.Reviewed = row.reviewed
		track.Replacement = replacement
	}

	if row.blockedCat != nil {
		detail := repairFailureDetail[*row.blockedCat]
		if detail == "" {
			detail = repairFailureDetail[repairUnknownCause]
		}
		track.Blocked = &RepairBlocked{Category: *row.blockedCat, Detail: detail}
		if row.blockedAt != nil {
			track.Blocked.Since = *row.blockedAt
		}
	}

	// A trusted active revision is the ordinary case and says nothing about repair.
	if row.activeRevision != nil && row.activeTrusted {
		track.State = repairReady
		track.Reason = fmt.Sprintf("Supported %s are available up to what you've read.", noun)
		return track
	}

	// Everything below is withheld. Report how much, so "no facts" is distinguishable
	// from "facts exist but are not trusted" — those look identical in the reader today.
	track.WithheldClaims = row.withheldClaims

	if track.Replacement == nil {
		if row.activeRevision == nil {
			track.State = repairUnavailable
			track.Reason = fmt.Sprintf("No reviewed %s have been built for this book yet.", noun)
			return track
		}
		track.State = repairQuarantined
		track.Reason = fmt.Sprintf(
			"%d %s are withheld: this book's knowledge is quarantined and nothing is replacing it yet. Translations are unaffected.",
			row.withheldClaims, noun)
		return track
	}

	// Order matters. Review can only be waiting on a human once EVERY chapter succeeded:
	// a rebuild sitting at 7 done / 3 failed has nothing to review, whether or not those
	// three will be retried. And a failure only ends the rebuild when nothing is left to
	// try — chapters still queued mean the run is simply not finished.
	unfinished := row.total - row.done - row.failed
	switch {
	case row.total == 0:
		track.State = repairQuarantined
		track.Reason = fmt.Sprintf(
			"%d %s are withheld. A replacement has been started but has no chapters to process yet.",
			row.withheldClaims, noun)
	case row.done == row.total:
		track.State = repairAwaitingReview
		hasHash := row.reviewHash != nil && *row.reviewHash != ""
		switch {
		case row.reviewed && !hasHash:
			// record_review stores its metrics and clears `review` in one step, so the
			// activation hash briefly does not exist. Saying so beats a button silently
			// disappearing, which is what this looked like before.
			track.Reason = fmt.Sprintf(
				"Reviewed. A fresh report is being taken before activation is possible; %s stay withheld until then.",
				noun)
		case row.reviewed:
			track.Reason = fmt.Sprintf(
				"Reviewed and ready to activate. %s stay withheld until it is activated.",
				capitalise(noun))
		default:
			track.Reason = fmt.Sprintf(
				"All %d chapters have been rebuilt. %s stay withheld until the result is reviewed and activated.",
				row.total, capitalise(noun))
		}
	case row.failed > 0 && !row.retryable && unfinished == 0:
		track.State = repairFailed
		track.Reason = fmt.Sprintf(
			"The replacement stopped with %d of %d chapters failed and no retries left. %s stay withheld until a fresh rebuild is started.",
			row.failed, row.total, capitalise(noun))
	default:
		track.State = repairRebuilding
		if track.Blocked != nil {
			// Leading with the blockage: "Rebuilding: 0 of 26" next to a dead endpoint is
			// the exact reading that sent someone to the journal to find out why.
			track.Reason = fmt.Sprintf(
				"Stalled at %d of %d chapters — %s. %s stay withheld until this is resolved.",
				row.done, row.total, track.Blocked.Detail, capitalise(noun))
			break
		}
		// Model calls, not claims: a claim only lands when a whole chapter publishes, so
		// quoting claims here would move at exactly the same time as the chapter counter
		// and tell the reader nothing about whether the current chapter is progressing.
		track.Reason = fmt.Sprintf(
			"Rebuilding: %d of %d chapters done%s, %d model calls completed. %s stay withheld until the rebuild is reviewed.",
			row.done, row.total, failedSuffix(row.failed), row.calls, capitalise(noun))
	}
	return track
}

func failedSuffix(failed int) string {
	if failed == 0 {
		return ""
	}
	return fmt.Sprintf(", %d failed", failed)
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

// operatorOutcome distinguishes the three states that matter for throttling. Absent is
// NOT a failure: it is what every ordinary reader polling repair status looks like, and
// counting it would let normal traffic lock the operator out.
type operatorOutcome int

const (
	operatorAbsent operatorOutcome = iota
	operatorValid
	operatorInvalid
)

// checkOperator evaluates the credential and maintains the throttle. It writes no
// response, so the advisory use (the status endpoint's `operator` field) and the enforcing
// use (repair writes) share exactly one implementation.
//
// This is a shared secret, not a user system — there is no per-operator identity here.
// It exists because reader identity is an unauthenticated X-Reader-ID header, and the
// only real credential in the system, INGEST_INTERNAL_TOKEN, has total authority over
// the database (including novel deletion) and must never reach a browser.
// limiter returns the throttle, constructing one on first use. main.go always supplies
// it, so the lazy path exists only for tests that build an API literal; it is not
// goroutine-safe on that first call and does not need to be.
func (a *API) limiter() *authThrottle {
	if a.throttle == nil {
		a.throttle = newAuthThrottle()
	}
	return a.throttle
}

func (a *API) checkOperator(r *http.Request) (operatorOutcome, time.Duration) {
	presented := strings.TrimSpace(r.Header.Get("X-Operator-Token"))
	if presented == "" {
		return operatorAbsent, 0
	}
	key := clientKey(r)
	if a.operatorToken == "" {
		// Nothing to compare against. Still a presented credential, so it is still an
		// attempt worth counting — a scan does not know the feature is disabled.
		return operatorInvalid, a.limiter().recordFailure(key)
	}
	// Check the block BEFORE comparing, so a throttled caller learns nothing from timing.
	if wait := a.limiter().blockedFor(key); wait > 0 {
		return operatorInvalid, wait
	}
	if subtle.ConstantTimeCompare([]byte(presented), []byte(a.operatorToken)) != 1 {
		return operatorInvalid, a.limiter().recordFailure(key)
	}
	a.limiter().recordSuccess(key)
	return operatorValid, 0
}

// operatorAllowed is the advisory form used by the status endpoint.
func (a *API) operatorAllowed(r *http.Request) bool {
	outcome, _ := a.checkOperator(r)
	return outcome == operatorValid
}

// requireOperator gates a repair write. Fails closed when no token is configured, so a
// deployment that never set one cannot be driven by an empty header.
func (a *API) requireOperator(w http.ResponseWriter, r *http.Request) bool {
	outcome, retryAfter := a.checkOperator(r)
	if outcome == operatorValid {
		return true
	}
	if retryAfter > 0 {
		seconds := int(retryAfter.Seconds())
		if seconds < 1 {
			seconds = 1
		}
		w.Header().Set("Retry-After", strconv.Itoa(seconds))
		writeError(w, http.StatusTooManyRequests, "too many failed operator attempts; try again later")
		return false
	}
	if a.operatorToken == "" {
		writeError(w, http.StatusServiceUnavailable, "repair operator access is not configured")
		return false
	}
	writeError(w, http.StatusForbidden, "operator access required")
	return false
}

func (a *API) getRepairStatus(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	status, err := a.store.RepairStatus(r.Context(), novelID)
	if err != nil {
		log.Printf("repair status: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load repair status")
		return
	}
	outcome, retryAfter := a.checkOperator(r)
	status.Operator = outcome == operatorValid
	status.OperatorThrottled = retryAfter > 0
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, status)
}

// postRepair records a repair intent. Operator-gated here, then proxied to ingest-api's
// token-gated route: the browser holds only the weaker operator secret, never the internal
// token. Mirrors postTranslateAhead's proxy shape.
func (a *API) postRepair(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if !a.requireOperator(w, r) {
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, paramsRequestLimit))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	// The client never chooses who asked. Same discipline as queueControl's changed_by and
	// approveCharacterName's reviewer: an audit label taken from the request, not the body.
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(body, &payload); err != nil {
		writeError(w, http.StatusBadRequest, "invalid repair request")
		return
	}
	actor := "unknown operator"
	if id, ok := readerID(r); ok {
		actor = id
	}
	encoded, _ := json.Marshal(actor)
	payload["requested_by"] = encoded
	body, _ = json.Marshal(payload)

	result, status, err := a.ingest.RequestRepair(r.Context(), novelID, body)
	if err != nil {
		log.Printf("request repair: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) deleteRepair(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if !a.requireOperator(w, r) {
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	requestID, ok := pathUUID(r, "request")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid request id")
		return
	}
	result, status, err := a.ingest.CancelRepair(r.Context(), novelID, requestID)
	if err != nil {
		log.Printf("cancel repair: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// getRepairPreview returns the frozen review report so a reviewer can assess claims
// against their source quotes in a browser.
//
// This is the one place in the repair surface that returns story content, and it is NOT
// spoiler-gated: the report carries quotes from every chapter in the snapshot regardless
// of anyone's progress. That is deliberate and necessary — a reviewer who could only see
// quotes up to their own reading position could not assess the claims they are being
// asked to approve — and it is why the operator check above is a hard requirement here
// rather than the advisory flag it is on the status endpoint.
// getRepairProgress shows what the rebuild has published so far. Operator-gated for the
// same reason as the preview: these are unreviewed claims carrying source quotes from
// anywhere in the book, so they are not spoiler-safe for an ordinary reader.
func (a *API) getRepairProgress(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if !a.requireOperator(w, r) {
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	facts, err := a.store.RepairProgress(r.Context(), novelID)
	if err != nil {
		log.Printf("repair progress: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load repair progress")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]any{"facts": facts})
}

func (a *API) getRepairPreview(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if !a.requireOperator(w, r) {
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	track := r.URL.Query().Get("track")
	if track == "" {
		track = "graph"
	}
	if track != "graph" && track != "events" {
		writeError(w, http.StatusBadRequest, "track must be graph or events")
		return
	}
	preview, err := a.store.RepairPreview(r.Context(), novelID, track)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "no revision is being rebuilt for this book")
		return
	}
	if err != nil {
		log.Printf("repair preview: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load repair preview")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, preview)
}
