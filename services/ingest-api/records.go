package main

// Records maintenance actions: the operator surface that replaced repair and chapter
// re-extraction. Retries re-run work in place; a rebuild starts a new generation and
// re-enriches chronologically, because published extraction content is immutable
// (§0.2) and a prompt/ontology/model change is a different generation, not an edit.

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
)

var (
	ErrRecordsRebuildActive   = errors.New("a records rebuild is already active")
	ErrRecordsRebuildConflict = errors.New("records rebuild cannot be discarded")
	ErrRecordsReviewInvalid   = errors.New("invalid records review request")
	ErrRecordsReviewConflict  = errors.New("records review request conflicts with an existing decision")
	ErrRecordsReviewNotFound  = errors.New("record is not available for review")
	ErrRecordsReviewStale     = errors.New("record review is stale; reload before reviewing")
)

const recordsAdvisoryLockSQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

// RecordsRebuildStatus is deliberately metadata-only: it lets an operator recover the
// generation id after a reload without exposing chapter text or knowledge rows.
type RecordsRebuildStatus struct {
	NovelID                 string  `json:"novel_id"`
	ActiveGenerationID      *string `json:"active_generation_id"`
	ActiveState             string  `json:"active_state"`
	PredecessorGenerationID *string `json:"predecessor_generation_id"`
	HasPredecessor          bool    `json:"has_predecessor"`
	EligibleChapters        int     `json:"eligible_chapters"`
	PublishedChapters       int     `json:"published_chapters"`
	MissingChapters         int     `json:"missing_chapters"`
	Discardable             bool    `json:"discardable"`
}

func lockRecordsNovel(ctx context.Context, tx pgx.Tx, novelID string) error {
	_, err := tx.Exec(ctx, recordsAdvisoryLockSQL, "records:"+novelID)
	return err
}

// recordsRebuildCounts is the single completion predicate shared by rebuild admission,
// discard, and the reload-safe status endpoint. A saved chapter is eligible once its
// translation is readable; a replacement is complete only when every eligible chapter
// has a published run. Empty eligible sets remain discardable while work has not begun.
func recordsRebuildCounts(ctx context.Context, tx pgx.Tx, novelID, generation string) (eligible, published, missing int, err error) {
	err = tx.QueryRow(ctx, `WITH eligible AS (
		SELECT c.chapter_index
		  FROM chapter c
		 WHERE c.novel_id=$1 AND (c.translation_ready OR c.status='done')
	), coverage AS (
		SELECT e.chapter_index, EXISTS (
			SELECT 1 FROM record_run r
			 WHERE r.novel_id=$1 AND r.generation_id=$2
			   AND r.chapter_index=e.chapter_index AND r.status='published'
		) AS covered FROM eligible e
	)
	SELECT count(*)::int,
	       count(*) FILTER (WHERE covered)::int,
	       count(*) FILTER (WHERE NOT covered)::int
	  FROM coverage`, novelID, generation).Scan(&eligible, &published, &missing)
	return
}

// retryRecords clears one chapter's failed run so the worker re-runs it. Published runs
// are left alone: rebuilding already-published extraction is a generation-level action.
func (s *Store) retryRecords(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin records retry: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	// Serialize against a publishing worker and against a concurrent reset.
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return fmt.Errorf("lock records retry: %w", err)
	}
	var generation string
	if err = tx.QueryRow(ctx,
		"SELECT active_record_generation::text FROM novel WHERE id=$1 FOR UPDATE", novelID,
	).Scan(&generation); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return pgx.ErrNoRows
		}
		return fmt.Errorf("find novel: %w", err)
	}
	if _, err = tx.Exec(ctx,
		"DELETE FROM record_run WHERE novel_id=$1 AND generation_id=$2 AND chapter_index=$3 AND status<>'published'",
		novelID, generation, chapter); err != nil {
		return fmt.Errorf("clear failed run: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit records retry: %w", err)
	}
	return s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: chapter, Priority: true, Enrichment: true, RecordGenerationID: generation})
}

// retryRendering cannot delete child rows from an already-published run: the publication
// is frozen as a whole, not just its record_run metadata. A rendering retry therefore
// opens a fresh generation and re-enriches chapters in order, just like a rebuild.
func (s *Store) retryRendering(ctx context.Context, novelID string, chapter int) error {
	_, _, err := s.rebuildRecords(ctx, novelID)
	return err
}

// rebuildRecords opens a fresh generation and re-enriches every saved chapter in order.
// The new generation becomes active immediately and starts empty: readers see pending
// knowledge while it fills, rather than a mix of two generations' identity decisions.
func (s *Store) rebuildRecords(ctx context.Context, novelID string) (string, int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return "", 0, fmt.Errorf("begin records rebuild: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return "", 0, fmt.Errorf("lock records rebuild: %w", err)
	}
	var ontology []byte
	var predecessor, activeState string
	var hasPredecessor bool
	if err = tx.QueryRow(ctx, `SELECT ontology,COALESCE(active_record_generation::text,'')
		FROM novel WHERE id=$1 FOR UPDATE`, novelID).Scan(&ontology, &predecessor); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", 0, pgx.ErrNoRows
		}
		return "", 0, fmt.Errorf("find novel: %w", err)
	}
	if predecessor == "" {
		return "", 0, fmt.Errorf("%w: novel has no predecessor generation", ErrRecordsRebuildConflict)
	}
	if err = tx.QueryRow(ctx, `SELECT g.state,g.predecessor_generation_id IS NOT NULL
		FROM record_generation g WHERE g.id=$1 AND g.novel_id=$2`, predecessor, novelID).
		Scan(&activeState, &hasPredecessor); err != nil {
		return "", 0, fmt.Errorf("check rebuild state: %w", err)
	}
	eligible, _, missing, err := recordsRebuildCounts(ctx, tx, novelID, predecessor)
	if err != nil {
		return "", 0, fmt.Errorf("check rebuild completion: %w", err)
	}
	// A generation with a predecessor is a replacement. It remains in progress until it
	// has at least one eligible chapter and every eligible chapter has a published run;
	// an empty replacement has not actually completed any work and is still fenced.
	// Once complete, it may itself be the predecessor of the next rebuild (the old
	// `predecessor_generation_id IS NOT NULL` test incorrectly wedged every novel after
	// its first rebuild).
	if hasPredecessor && activeState == "active" && (eligible == 0 || missing > 0) {
		return "", 0, ErrRecordsRebuildActive
	}
	var predecessorArg any = predecessor
	var generation string
	if err = tx.QueryRow(ctx, `INSERT INTO record_generation
		(novel_id,state,ontology,prompt_version,checks_version,extraction_model,source_lang,target_lang,predecessor_generation_id)
		SELECT id,'active',ontology,'records-v1','records-v1','',source_lang,target_lang,$2
		  FROM novel WHERE id=$1
		RETURNING id::text`, novelID, predecessorArg).Scan(&generation); err != nil {
		return "", 0, fmt.Errorf("create generation: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='retired'
		WHERE novel_id=$1 AND id<>$2 AND state='active'`, novelID, generation); err != nil {
		return "", 0, fmt.Errorf("retire generations: %w", err)
	}
	if _, err = tx.Exec(ctx, "UPDATE novel SET active_record_generation=$2 WHERE id=$1",
		novelID, generation); err != nil {
		return "", 0, fmt.Errorf("activate generation: %w", err)
	}
	rows, err := tx.Query(ctx, "SELECT chapter_index FROM chapter WHERE novel_id=$1 ORDER BY chapter_index", novelID)
	if err != nil {
		return "", 0, fmt.Errorf("list chapters: %w", err)
	}
	chapters := []int{}
	for rows.Next() {
		var index int
		if err := rows.Scan(&index); err != nil {
			rows.Close()
			return "", 0, err
		}
		chapters = append(chapters, index)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return "", 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return "", 0, fmt.Errorf("commit records rebuild: %w", err)
	}
	// Chronological order matters: who's-who resolves a chapter against the identities
	// published before it, so enriching out of order would resolve against nothing.
	for _, index := range chapters {
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index, Enrichment: true, RecordGenerationID: generation}); err != nil {
			return generation, 0, err
		}
	}
	return generation, len(chapters), nil
}

// discardRecordsRebuild restores exactly the generation that was active when the
// replacement began. Published replacement runs are immutable history: they remain in
// the retired generation while the predecessor becomes active again. The novel advisory
// lock fences this transaction against both a publisher and another rebuild/discard request.
func (s *Store) discardRecordsRebuild(ctx context.Context, novelID, expectedGeneration string) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin records discard: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return fmt.Errorf("lock records discard: %w", err)
	}
	var active, predecessor, activeState, predecessorState string
	err = tx.QueryRow(ctx, `
		SELECT g.id::text,g.predecessor_generation_id::text,g.state,COALESCE(p.state,'')
		  FROM novel n
		  JOIN record_generation g ON g.id=n.active_record_generation
		  LEFT JOIN record_generation p ON p.id=g.predecessor_generation_id
		 WHERE n.id=$1
		 FOR UPDATE OF n,g`, novelID).Scan(&active, &predecessor, &activeState, &predecessorState)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgx.ErrNoRows
	}
	if err != nil {
		return fmt.Errorf("find active rebuild: %w", err)
	}
	if active != expectedGeneration || predecessor == "" || activeState != "active" || predecessorState != "retired" {
		return ErrRecordsRebuildConflict
	}
	eligible, _, missing, err := recordsRebuildCounts(ctx, tx, novelID, active)
	if err != nil {
		return fmt.Errorf("check rebuild completion: %w", err)
	}
	if eligible > 0 && missing == 0 {
		return fmt.Errorf("%w: replacement is complete", ErrRecordsRebuildConflict)
	}
	if _, err = tx.Exec(ctx, `UPDATE novel SET active_record_generation=$2 WHERE id=$1`, novelID, predecessor); err != nil {
		return fmt.Errorf("restore predecessor: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='active',retired_at=NULL WHERE id=$1`, predecessor); err != nil {
		return fmt.Errorf("reactivate predecessor: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='retired',retired_at=now()
		WHERE id=$1 AND novel_id=$2`, active, novelID); err != nil {
		return fmt.Errorf("retire unfinished replacement: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit records discard: %w", err)
	}
	return nil
}

func (s *Store) recordsRebuildStatus(ctx context.Context, novelID string) (RecordsRebuildStatus, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("begin records rebuild status: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("lock records rebuild status: %w", err)
	}
	var out RecordsRebuildStatus
	var active, predecessor *string
	if err = tx.QueryRow(ctx, `SELECT n.active_record_generation::text,COALESCE(g.state,''),
		g.predecessor_generation_id::text
		FROM novel n LEFT JOIN record_generation g ON g.id=n.active_record_generation
		WHERE n.id=$1`, novelID).Scan(&active, &out.ActiveState, &predecessor); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return RecordsRebuildStatus{}, pgx.ErrNoRows
		}
		return RecordsRebuildStatus{}, fmt.Errorf("find records generation: %w", err)
	}
	out.NovelID = novelID
	out.ActiveGenerationID = active
	out.PredecessorGenerationID = predecessor
	out.HasPredecessor = predecessor != nil
	if active != nil {
		out.EligibleChapters, out.PublishedChapters, out.MissingChapters, err = recordsRebuildCounts(ctx, tx, novelID, *active)
		if err != nil {
			return RecordsRebuildStatus{}, fmt.Errorf("count records rebuild: %w", err)
		}
	}
	out.Discardable = active != nil && predecessor != nil && out.ActiveState == "active" &&
		(out.EligibleChapters == 0 || out.MissingChapters > 0)
	if err = tx.Commit(ctx); err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("commit records rebuild status: %w", err)
	}
	return out, nil
}

func (a *API) recordsRetry(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.retryRecords(r.Context(), r.PathValue("id"), chapter)
	a.writeRecordsAction(w, "retry", map[string]any{"retried": true, "chapter_index": chapter}, err)
}

func (a *API) recordsRenderRetry(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.retryRendering(r.Context(), r.PathValue("id"), chapter)
	a.writeRecordsAction(w, "render-retry", map[string]any{"retried": true, "chapter_index": chapter}, err)
}

func (a *API) recordsRebuild(w http.ResponseWriter, r *http.Request) {
	generation, chapters, err := a.store.rebuildRecords(r.Context(), r.PathValue("id"))
	a.writeRecordsAction(w, "rebuild",
		map[string]any{"generation_id": generation, "chapters_enqueued": chapters}, err)
}

func (a *API) recordsRebuildStatus(w http.ResponseWriter, r *http.Request) {
	status, err := a.store.recordsRebuildStatus(r.Context(), r.PathValue("id"))
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case err != nil:
		log.Printf("records rebuild status: %v", err)
		writeErr(w, http.StatusInternalServerError, "records status failed")
	default:
		writeJSON(w, http.StatusOK, status)
	}
}

func (a *API) recordsRebuildDiscard(w http.ResponseWriter, r *http.Request) {
	var request struct {
		GenerationID string `json:"generation_id"`
	}
	if err := decodeJSONBody(r, &request); err != nil || strings.TrimSpace(request.GenerationID) == "" {
		writeErr(w, http.StatusBadRequest, "generation_id is required")
		return
	}
	if _, err := uuid.Parse(request.GenerationID); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid generation_id")
		return
	}
	err := a.store.discardRecordsRebuild(r.Context(), r.PathValue("id"), request.GenerationID)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case errors.Is(err, ErrRecordsRebuildConflict), errors.Is(err, ErrRecordsRebuildActive):
		writeErr(w, http.StatusConflict, err.Error())
	case err != nil:
		log.Printf("records rebuild discard: %v", err)
		writeErr(w, http.StatusInternalServerError, "records action failed")
	default:
		writeJSON(w, http.StatusOK, map[string]any{"discarded": true, "generation_id": request.GenerationID})
	}
}

func (a *API) writeRecordsAction(w http.ResponseWriter, action string, body map[string]any, err error) {
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case err != nil:
		log.Printf("records %s: %v", action, err)
		writeErr(w, http.StatusInternalServerError, "records action failed")
	default:
		writeJSON(w, http.StatusOK, body)
	}
}
