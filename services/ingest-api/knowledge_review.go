package main

// Phase D write path: pass/reject held fact, edge and event rows for one chapter
// (migration 0074). This is the writer half of the review workspace — reader-api
// authorizes the reader's own stored reading position and proxies here over the
// bearer-token-gated route, mirroring facts.go's editFact and vocabulary.go's
// MutateVocabulary (advisory-lock the novel, FOR UPDATE the revision, reject a stale
// expected version, one audit row per change, bump graph_revision.version).

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

var (
	ErrKnowledgeReviewInvalid  = errors.New("invalid knowledge review request")
	ErrKnowledgeReviewStale    = errors.New("knowledge changed; reload before reviewing")
	ErrKnowledgeReviewNotFound = errors.New("no review revision available for this chapter")
)

type knowledgeReviewItem struct {
	ItemType string `json:"item_type"` // fact|edge|event
	ID       int64  `json:"id"`
	Verdict  string `json:"verdict"` // pass|reject
	Reason   string `json:"reason"`
}

type knowledgeReviewRequest struct {
	RevisionID string `json:"revision_id"`
	Version    int64  `json:"version"`
	Chapter    int    `json:"chapter"`
	Actor      string `json:"actor"`
	// RequestID is the caller's idempotency token for this whole logical action. Per-item
	// idempotency keys are derived from it (request_id:item_type:id) because
	// knowledge_review_audit's uniqueness is (novel_id, idempotency_key) and one review
	// call writes one audit row per item.
	RequestID string `json:"request_id"`
	// Items is the explicit selection. Exactly one of Items / PassAllCorroborated must be
	// set — never both, never neither (plan's "explicit chapter selection or a
	// pass_all_corroborated selection").
	Items               []knowledgeReviewItem `json:"items,omitempty"`
	PassAllCorroborated bool                  `json:"pass_all_corroborated,omitempty"`
}

type knowledgeReviewOutcome struct {
	ItemType       string `json:"item_type"`
	ID             int64  `json:"id"`
	Verdict        string `json:"verdict"`
	AlreadyApplied bool   `json:"already_applied"`
}

type knowledgeReviewResult struct {
	RevisionID string                   `json:"revision_id"`
	Version    int64                    `json:"version"`
	Applied    []knowledgeReviewOutcome `json:"applied"`
}

// reviewTarget names the base table, its id and chapter columns, and the audit FK column
// for one item type. novel_id/revision_id predicates are added by the caller; keeping
// them out of this table avoids ever building a query that forgets them.
type reviewTarget struct {
	table, chapterColumn, auditColumn string
}

var reviewTargets = map[string]reviewTarget{
	"fact":  {"fact", "source_chapter", "fact_id"},
	"edge":  {"edge", "source_chapter", "edge_id"},
	"event": {"event", "chapter_index", "event_id"},
}

// req is a pointer because the trims below must reach the audit rows and idempotency
// keys the caller derives, not just the checks here.
func validateKnowledgeReviewRequest(novelID string, req *knowledgeReviewRequest) error {
	if _, err := uuid.Parse(novelID); err != nil {
		return fmt.Errorf("%w: invalid novel id", ErrKnowledgeReviewInvalid)
	}
	if _, err := uuid.Parse(req.RevisionID); err != nil || req.Version <= 0 {
		return fmt.Errorf("%w: revision_id and version are required", ErrKnowledgeReviewInvalid)
	}
	if req.Chapter < 0 {
		return fmt.Errorf("%w: chapter must be nonnegative", ErrKnowledgeReviewInvalid)
	}
	req.Actor = strings.TrimSpace(req.Actor)
	req.RequestID = strings.TrimSpace(req.RequestID)
	if req.Actor == "" || len(req.Actor) > 200 {
		return fmt.Errorf("%w: actor is required", ErrKnowledgeReviewInvalid)
	}
	if req.RequestID == "" || len(req.RequestID) > 200 {
		return fmt.Errorf("%w: request_id is required", ErrKnowledgeReviewInvalid)
	}
	if (len(req.Items) > 0) == req.PassAllCorroborated {
		return fmt.Errorf("%w: exactly one of items or pass_all_corroborated is required", ErrKnowledgeReviewInvalid)
	}
	for _, item := range req.Items {
		if _, ok := reviewTargets[item.ItemType]; !ok {
			return fmt.Errorf("%w: unknown item_type %q", ErrKnowledgeReviewInvalid, item.ItemType)
		}
		if item.ID <= 0 {
			return fmt.Errorf("%w: invalid item id", ErrKnowledgeReviewInvalid)
		}
		if item.Verdict != "pass" && item.Verdict != "reject" {
			return fmt.Errorf("%w: verdict must be pass or reject", ErrKnowledgeReviewInvalid)
		}
		if strings.TrimSpace(item.Reason) == "" || len(item.Reason) > 2000 {
			return fmt.Errorf("%w: reason is required", ErrKnowledgeReviewInvalid)
		}
	}
	return nil
}

func verdictState(verdict string) string {
	if verdict == "pass" {
		return "passed"
	}
	return "rejected"
}

// applyReviewItem updates one held row and writes its audit trail, or reports an
// already-applied idempotent outcome. It never accepts a bare `WHERE id=$1` — novel,
// revision and chapter are always part of the predicate (plan's explicit warning).
func applyReviewItem(
	ctx context.Context, tx pgx.Tx, novelID, revisionID string, chapter int,
	requestID, actor string, item knowledgeReviewItem,
) (knowledgeReviewOutcome, error) {
	target := reviewTargets[item.ItemType]
	newState := verdictState(item.Verdict)
	idempotencyKey := fmt.Sprintf("%s:%s:%d", requestID, item.ItemType, item.ID)
	outcome := knowledgeReviewOutcome{ItemType: item.ItemType, ID: item.ID, Verdict: item.Verdict}

	var existingNewState string
	err := tx.QueryRow(ctx,
		`SELECT new_state FROM knowledge_review_audit WHERE novel_id=$1 AND idempotency_key=$2`,
		novelID, idempotencyKey).Scan(&existingNewState)
	if err == nil {
		if existingNewState != newState {
			return outcome, fmt.Errorf("%w: request_id %q was already used for %s %d with a different verdict",
				ErrKnowledgeReviewInvalid, requestID, item.ItemType, item.ID)
		}
		outcome.AlreadyApplied = true
		return outcome, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return outcome, err
	}

	var currentState string
	lockSQL := fmt.Sprintf(
		`SELECT review_state FROM %s WHERE id=$1 AND novel_id=$2 AND revision_id=$3 AND %s=$4 FOR UPDATE`,
		target.table, target.chapterColumn)
	err = tx.QueryRow(ctx, lockSQL, item.ID, novelID, revisionID, chapter).Scan(&currentState)
	if errors.Is(err, pgx.ErrNoRows) {
		return outcome, fmt.Errorf("%w: %s %d is not held in this novel/revision/chapter",
			ErrKnowledgeReviewInvalid, item.ItemType, item.ID)
	}
	if err != nil {
		return outcome, err
	}
	if currentState != "held" {
		// Not held, and no matching audit row was found above -- this selection is stale
		// or out of scope, not a legitimate replay. Fail the whole request rather than
		// silently skip (plan: "reject mixed/out-of-scope selections atomically").
		return outcome, fmt.Errorf("%w: %s %d is no longer held (state=%s)",
			ErrKnowledgeReviewInvalid, item.ItemType, item.ID, currentState)
	}

	// The row is already proven in scope and locked by the SELECT above, so these
	// predicates are redundant today. They are repeated anyway: an unscoped UPDATE that is
	// safe only because of a lock taken twenty lines earlier is one refactor away from
	// being the cross-novel write the plan warns about.
	updateSQL := fmt.Sprintf(
		`UPDATE %s SET review_state=$1 WHERE id=$2 AND novel_id=$3 AND revision_id=$4 AND %s=$5 AND review_state='held'`,
		target.table, target.chapterColumn)
	if _, err = tx.Exec(ctx, updateSQL, newState, item.ID, novelID, revisionID, chapter); err != nil {
		return outcome, err
	}
	auditSQL := fmt.Sprintf(
		`INSERT INTO knowledge_review_audit
			(novel_id,revision_id,%s,actor,old_state,new_state,action,reason,request_id,idempotency_key)
		 VALUES ($1,$2,$3,$4,'held',$5,$6,$7,$8,$9)`, target.auditColumn)
	if _, err = tx.Exec(ctx, auditSQL, novelID, revisionID, item.ID, actor, newState, item.Verdict, item.Reason, requestID, idempotencyKey); err != nil {
		return outcome, err
	}
	return outcome, nil
}

// corroboratedFactIDs is B.2.5's bulk-pass rule: a (entity, attribute) group qualifies
// only when every chapter-visible row in it shares the same normalized assertion
// signature (so "alive" and "dead" -- different signatures -- can never corroborate each
// other, and any genuine disagreement excludes the WHOLE group rather than picking a
// majority value), spans at least two distinct chapters the reviewer has actually read,
// and carries no review_flag anywhere in the group. Scoped to facts sourced at exactly
// this chapter, since the endpoint itself is chapter-scoped.
//
// The rule itself lives in migration 0076's corroborated_fact_ids SQL function, not here
// -- reader-api's read-only preview (reader_held_knowledge_bulk_eligible) calls the exact
// same function, so the preview a reviewer approves and what this write actually applies
// can never drift apart. This only covers `fact` rows: the ledger is keyed on
// (entity_id, attribute), which has no equivalent for edges or plain events.
func corroboratedFactIDs(ctx context.Context, tx pgx.Tx, novelID, revisionID string, chapter int) ([]int64, error) {
	rows, err := tx.Query(ctx,
		`SELECT fact_id FROM corroborated_fact_ids($1,$2,$3) ORDER BY fact_id`,
		novelID, revisionID, chapter)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (s *Store) reviewChapterKnowledge(ctx context.Context, novelID string, req knowledgeReviewRequest) (knowledgeReviewResult, error) {
	if err := validateKnowledgeReviewRequest(novelID, &req); err != nil {
		return knowledgeReviewResult{}, err
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return knowledgeReviewResult{}, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return knowledgeReviewResult{}, err
	}

	// The review revision is whichever one reader_held_knowledge (0074) would have picked:
	// the staging revision if one exists, else the active trusted one. Inlined rather than
	// called through that function because it is a SECURITY DEFINER grant to rls_reader
	// only, and this is an ordinary business join over graph_revision, not an RLS/spoiler
	// gate -- the same class of query editFact and startChapterReextract already do
	// directly in this package.
	var currentRevision string
	var currentVersion int64
	err = tx.QueryRow(ctx, `
		SELECT r.id::text, r.version
		  FROM graph_revision r
		 WHERE r.novel_id = $1
		   AND ((r.state = 'staging') OR (r.state = 'active' AND r.trusted))
		 ORDER BY CASE WHEN r.state = 'staging' THEN 0 ELSE 1 END, r.created_at DESC, r.id DESC
		 LIMIT 1
		 FOR UPDATE`, novelID).Scan(&currentRevision, &currentVersion)
	if errors.Is(err, pgx.ErrNoRows) {
		return knowledgeReviewResult{}, ErrKnowledgeReviewNotFound
	}
	if err != nil {
		return knowledgeReviewResult{}, err
	}
	if currentRevision != req.RevisionID || currentVersion != req.Version {
		return knowledgeReviewResult{}, ErrKnowledgeReviewStale
	}

	items := req.Items
	if req.PassAllCorroborated {
		ids, err := corroboratedFactIDs(ctx, tx, novelID, currentRevision, req.Chapter)
		if err != nil {
			return knowledgeReviewResult{}, err
		}
		items = make([]knowledgeReviewItem, 0, len(ids))
		for _, id := range ids {
			items = append(items, knowledgeReviewItem{
				ItemType: "fact", ID: id, Verdict: "pass",
				Reason: "bulk pass: same assertion corroborated across >=2 chapters already read (B.2.5)",
			})
		}
	}

	result := knowledgeReviewResult{RevisionID: currentRevision, Version: currentVersion, Applied: []knowledgeReviewOutcome{}}
	appliedAny := false
	for _, item := range items {
		outcome, err := applyReviewItem(ctx, tx, novelID, currentRevision, req.Chapter, req.RequestID, req.Actor, item)
		if err != nil {
			return knowledgeReviewResult{}, err
		}
		if !outcome.AlreadyApplied {
			appliedAny = true
		}
		result.Applied = append(result.Applied, outcome)
	}

	if appliedAny {
		if err = tx.QueryRow(ctx, `UPDATE graph_revision SET version=version+1 WHERE id=$1 RETURNING version`, currentRevision).Scan(&result.Version); err != nil {
			return knowledgeReviewResult{}, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return knowledgeReviewResult{}, err
	}
	return result, nil
}

func (a *API) reviewChapterKnowledge(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	var body knowledgeReviewRequest
	if err = decodeJSONBody(r, &body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	body.Chapter = chapter
	result, err := a.store.reviewChapterKnowledge(r.Context(), r.PathValue("id"), body)
	var pgErr *pgconn.PgError
	switch {
	case errors.Is(err, ErrKnowledgeReviewNotFound):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrKnowledgeReviewStale):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrKnowledgeReviewInvalid):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.As(err, &pgErr) && pgErr.Code == "23505":
		writeErr(w, http.StatusConflict, "this review was already submitted")
	case err != nil:
		log.Printf("review chapter knowledge: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not update chapter knowledge review")
	default:
		writeJSON(w, http.StatusOK, result)
	}
}
