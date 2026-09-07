package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"

	"github.com/jackc/pgx/v5"
)

// deleteGraph removes every revision and all graph-derived rows for one novel while
// preserving the novel, its chapter bodies, translations, glossary, and reader progress.
// This is an explicit lifecycle action, not a pipeline rewrite of append-only facts
// (instructions.md §0.2). The FK graph owns the derived-data ordering (migration 0030).
func (s *Store) deleteGraph(ctx context.Context, novelID string) (int64, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin graph deletion: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()

	// Serialize with repair requests for this novel. A running worker may finish its
	// inference call, but the deleted revision fences any later publication.
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return 0, fmt.Errorf("lock graph deletion: %w", err)
	}
	var lockedID string
	if err = tx.QueryRow(ctx, "SELECT id::text FROM novel WHERE id=$1 FOR UPDATE", novelID).Scan(&lockedID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, pgx.ErrNoRows
		}
		return 0, fmt.Errorf("find novel: %w", err)
	}

	// Break the novel -> active revision pointer before cascading the revision subtree.
	if _, err = tx.Exec(ctx, "UPDATE novel SET active_graph_revision=NULL WHERE id=$1", novelID); err != nil {
		return 0, fmt.Errorf("clear active graph: %w", err)
	}
	// A queued/running graph request must not recreate or continue work after the reader
	// has explicitly deleted the graph. Completed request history is retained as metadata.
	if _, err = tx.Exec(ctx, `UPDATE repair_request
		SET state='failed',category='cancelled',updated_at=now()
		WHERE novel_id=$1 AND track='graph' AND state IN ('pending','running')`, novelID); err != nil {
		return 0, fmt.Errorf("cancel graph repair: %w", err)
	}
	// Structured event arguments own their source-language surface independently of the
	// entity graph. Unlink them first so the graph FK cascade cannot erase useful event
	// participants along with the entities they used to resolve to.
	if _, err = tx.Exec(ctx, `UPDATE chapter_event_argument
		SET entity_id=NULL,linked_graph_revision=NULL
		WHERE novel_id=$1 AND linked_graph_revision IS NOT NULL`, novelID); err != nil {
		return 0, fmt.Errorf("unlink event arguments: %w", err)
	}
	tag, err := tx.Exec(ctx, "DELETE FROM graph_revision WHERE novel_id=$1", novelID)
	if err != nil {
		return 0, fmt.Errorf("delete graph revisions: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit graph deletion: %w", err)
	}
	return tag.RowsAffected(), nil
}

func (a *API) deleteGraph(w http.ResponseWriter, r *http.Request) {
	deleted, err := a.store.deleteGraph(r.Context(), r.PathValue("id"))
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case err != nil:
		log.Printf("deleteGraph: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not delete graph")
	default:
		writeJSON(w, http.StatusOK, map[string]any{"deleted": true, "revisions_deleted": deleted})
	}
}
