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

	"github.com/jackc/pgx/v5"
)

// retryRecords clears one chapter's failed run so the worker re-runs it. Published runs
// are left alone: rebuilding already-published extraction is a generation-level action.
func (s *Store) retryRecords(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin records retry: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	// Serialize against a publishing worker and against a concurrent reset.
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
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
	return s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: chapter, Priority: true, Enrichment: true})
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
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return "", 0, fmt.Errorf("lock records rebuild: %w", err)
	}
	var ontology []byte
	if err = tx.QueryRow(ctx, "SELECT ontology FROM novel WHERE id=$1 FOR UPDATE", novelID).Scan(&ontology); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", 0, pgx.ErrNoRows
		}
		return "", 0, fmt.Errorf("find novel: %w", err)
	}
	var generation string
	if err = tx.QueryRow(ctx, `INSERT INTO record_generation
		(novel_id,state,ontology,prompt_version,checks_version,extraction_model,source_lang,target_lang)
		SELECT id,'active',ontology,'records-v1','records-v1','',source_lang,target_lang
		  FROM novel WHERE id=$1
		RETURNING id::text`, novelID).Scan(&generation); err != nil {
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
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index, Enrichment: true}); err != nil {
			return generation, 0, err
		}
	}
	return generation, len(chapters), nil
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
