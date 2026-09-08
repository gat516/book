package main

// Knowledge repair requests (migration 0043).
//
// This file records what a human asked for; it does not repair anything. The actions —
// prepare, review, activate, rollback, extend — are implemented once, in Python, by
// pipeline/graph_rebuild.py and pipeline/event_rebuild.py, and executed by
// pipeline/repair.py on the worker's idle tick.
//
// Nothing here may validate a review document or judge whether a revision is good enough
// to activate. record_review deliberately derives its metrics from per-item assessments
// joined against actually-stored bindings, and qualified() holds the §0 thresholds. A
// second copy of either in Go would drift from the first — glossary_hash_test.go exists
// because exactly that happened with the glossary hash chain. So the checks below are
// strictly structural: is this a known track, a known action, a real novel, a revision
// that exists on the right table, and is there already something in flight.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var (
	ErrRepairInProgress      = errors.New("this book already has processing underway; check its status before starting another action")
	ErrRepairInvalid         = errors.New("invalid repair request")
	ErrRepairNovelUnknown    = errors.New("no such novel")
	ErrRepairRevisionUnknown = errors.New("no such revision for this book")
)

type RepairRequestView struct {
	ID          string `json:"id"`
	NovelID     string `json:"novel_id"`
	Track       string `json:"track"`
	Action      string `json:"action"`
	RevisionID  string `json:"revision_id,omitempty"`
	State       string `json:"state"`
	RequestedBy string `json:"requested_by"`
}

type repairRequestBody struct {
	Track      string `json:"track"`
	Action     string `json:"action"`
	RevisionID string `json:"revision_id"`
	// Set only for chapter-scoped re-extraction actions.
	ChapterIndex *int            `json:"chapter_index"`
	Params       json.RawMessage `json:"params"`
	RequestedBy  string          `json:"requested_by"`
}

// paramsLimit keeps a review document — the only genuinely large params payload — from
// becoming an unbounded write. A megabyte leaves ample room for an exhaustive small-book
// review while retaining a firm request bound.
const paramsLimit = 1 << 20

func validRepairTrack(track string) bool {
	return track == "graph" || track == "events"
}

func validRepairAction(action string) bool {
	switch action {
	case "prepare", "review", "activate", "rollback", "reextract", "reextract_apply", "discard", "extend", "retry":
		return true
	}
	return false
}

// RequestRepair records one repair intent. It follows ApproveCharacterName's shape — begin,
// take the novel-wide advisory lock, verify, insert, commit — but deliberately does NOT
// enqueue anything to Redis afterwards. The worker polls repair_request on its idle tick,
// because repair must always lose to reader-critical translation work.
func (s *Store) RequestRepair(ctx context.Context, novelID string, body repairRequestBody) (RepairRequestView, error) {
	if _, err := uuid.Parse(novelID); err != nil {
		return RepairRequestView{}, fmt.Errorf("%w: novel id is not a uuid", ErrRepairInvalid)
	}
	track := strings.TrimSpace(body.Track)
	action := strings.TrimSpace(body.Action)
	if !validRepairTrack(track) {
		return RepairRequestView{}, fmt.Errorf("%w: track must be graph or events", ErrRepairInvalid)
	}
	if !validRepairAction(action) {
		return RepairRequestView{}, fmt.Errorf("%w: unknown repair action", ErrRepairInvalid)
	}
	requestedBy := strings.TrimSpace(body.RequestedBy)
	if requestedBy == "" || len(requestedBy) > 200 {
		return RepairRequestView{}, fmt.Errorf("%w: requested_by is required", ErrRepairInvalid)
	}

	// reextract is chapter-scoped and targets whatever revision is currently active, so it
	// names a chapter instead of a revision. The two are mutually exclusive, and the table
	// has a CHECK saying so.
	chapterScoped := action == "reextract" || action == "reextract_apply"
	if chapterScoped != (body.ChapterIndex != nil) {
		return RepairRequestView{}, fmt.Errorf(
			"%w: re-extraction actions require chapter_index, and only they may set it", ErrRepairInvalid)
	}
	if body.ChapterIndex != nil && *body.ChapterIndex < 0 {
		return RepairRequestView{}, fmt.Errorf("%w: chapter_index must be nonnegative", ErrRepairInvalid)
	}

	revisionID := strings.TrimSpace(body.RevisionID)
	// prepare is the action that CREATES a revision, so it cannot name one; every other
	// action operates on a revision that already exists.
	withoutRevision := action == "prepare" || action == "extend" || chapterScoped
	if withoutRevision && revisionID != "" {
		return RepairRequestView{}, fmt.Errorf("%w: %s must not name a revision", ErrRepairInvalid, action)
	}
	if !withoutRevision {
		if _, err := uuid.Parse(revisionID); err != nil {
			return RepairRequestView{}, fmt.Errorf("%w: %s requires a revision_id", ErrRepairInvalid, action)
		}
	}

	params := body.Params
	if len(params) == 0 {
		params = json.RawMessage(`{}`)
	}
	if len(params) > paramsLimit {
		return RepairRequestView{}, fmt.Errorf("%w: params is too large", ErrRepairInvalid)
	}
	if !json.Valid(params) {
		return RepairRequestView{}, fmt.Errorf("%w: params must be a JSON object", ErrRepairInvalid)
	}
	var paramsObject map[string]json.RawMessage
	if err := json.Unmarshal(params, &paramsObject); err != nil {
		return RepairRequestView{}, fmt.Errorf("%w: params must be a JSON object", ErrRepairInvalid)
	}
	if raw, ok := paramsObject["upto_chapter"]; ok {
		var ceiling int
		if err := json.Unmarshal(raw, &ceiling); err != nil || ceiling < 0 || ceiling > 2147483647 {
			return RepairRequestView{}, fmt.Errorf("%w: upto_chapter must be a nonnegative integer", ErrRepairInvalid)
		}
		if track != "graph" || (action != "prepare" && action != "extend") {
			return RepairRequestView{}, fmt.Errorf("%w: upto_chapter applies only to graph prepare or extend", ErrRepairInvalid)
		}
	} else if action == "extend" {
		return RepairRequestView{}, fmt.Errorf("%w: extend requires upto_chapter", ErrRepairInvalid)
	}
	if action == "extend" && track != "graph" {
		return RepairRequestView{}, fmt.Errorf("%w: extend applies only to the graph track", ErrRepairInvalid)
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return RepairRequestView{}, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	// Same novel-wide serialization every other write path here takes, so two operators
	// acting at once queue behind each other rather than interleaving.
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return RepairRequestView{}, err
	}

	var exists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM novel WHERE id=$1)`, novelID).Scan(&exists); err != nil {
		return RepairRequestView{}, err
	}
	if !exists {
		return RepairRequestView{}, ErrRepairNovelUnknown
	}
	if action == "extend" {
		if err := tx.QueryRow(ctx, `SELECT EXISTS(
			SELECT 1 FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision
			WHERE n.id=$1 AND r.state='active' AND r.trusted AND NOT r.legacy)`, novelID).Scan(&exists); err != nil {
			return RepairRequestView{}, err
		}
		if !exists {
			return RepairRequestView{}, ErrRepairRevisionUnknown
		}
	}

	// The revision column carries no foreign key, because 'graph' rows point at
	// graph_revision and 'events' rows at event_revision. Check it against the right
	// table here rather than letting the executor discover it minutes later.
	if revisionID != "" {
		table := "graph_revision"
		if track == "events" {
			table = "event_revision"
		}
		var found bool
		query := fmt.Sprintf(`SELECT EXISTS(SELECT 1 FROM %s WHERE id=$1 AND novel_id=$2)`, table)
		if err := tx.QueryRow(ctx, query, revisionID, novelID).Scan(&found); err != nil {
			return RepairRequestView{}, err
		}
		if !found {
			return RepairRequestView{}, ErrRepairRevisionUnknown
		}
	}

	view := RepairRequestView{
		NovelID: novelID, Track: track, Action: action,
		RevisionID: revisionID, State: "pending", RequestedBy: requestedBy,
	}
	var revisionArg any
	if revisionID != "" {
		revisionArg = revisionID
	}
	err = tx.QueryRow(ctx, `INSERT INTO repair_request
		(novel_id, track, action, revision_id, params, requested_by, chapter_index)
		VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id::text`,
		novelID, track, action, revisionArg, params, requestedBy, body.ChapterIndex).Scan(&view.ID)
	if err != nil {
		// repair_request_one_active: one outstanding action per novel per track. A second
		// click while a rebuild is starting is a duplicate key, not a race.
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return RepairRequestView{}, ErrRepairInProgress
		}
		return RepairRequestView{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return RepairRequestView{}, err
	}
	return view, nil
}

// CancelRepairRequest withdraws a request that has not started. A running one is left
// alone: the executor holds locks and may be mid-transaction, and there is no safe way to
// interrupt prepare's quarantine or switch's cutover from the outside.
func (s *Store) CancelRepairRequest(ctx context.Context, novelID, requestID string) error {
	tag, err := s.db.Exec(ctx,
		`UPDATE repair_request SET state='failed', category='cancelled', updated_at=now()
		  WHERE id=$1 AND novel_id=$2 AND state='pending'`, requestID, novelID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return pgx.ErrNoRows
	}
	return nil
}

func (a *API) requestRepair(w http.ResponseWriter, r *http.Request) {
	var body repairRequestBody
	if err := decodeJSONBody(r, &body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	view, err := a.store.RequestRepair(r.Context(), r.PathValue("id"), body)
	switch {
	case errors.Is(err, ErrRepairNovelUnknown):
		writeErr(w, http.StatusNotFound, err.Error())
		return
	case errors.Is(err, ErrRepairRevisionUnknown):
		writeErr(w, http.StatusNotFound, err.Error())
		return
	case errors.Is(err, ErrRepairInProgress):
		writeErr(w, http.StatusConflict, err.Error())
		return
	case errors.Is(err, ErrRepairInvalid):
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	case err != nil:
		log.Printf("request repair: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not record repair request")
		return
	}
	writeJSON(w, http.StatusAccepted, view)
}

func (a *API) cancelRepair(w http.ResponseWriter, r *http.Request) {
	err := a.store.CancelRepairRequest(r.Context(), r.PathValue("id"), r.PathValue("request"))
	if errors.Is(err, pgx.ErrNoRows) {
		writeErr(w, http.StatusNotFound, "no pending repair request with that id")
		return
	}
	if err != nil {
		log.Printf("cancel repair: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not cancel repair request")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
