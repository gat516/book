package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"strings"
)

// ErrFactNotFound refuses a retraction for a fact that doesn't exist.
var ErrFactNotFound = errors.New("no such fact")

type factRetractionRequest struct {
	ChapterIndex  int    `json:"chapter_index"`
	PromptVersion string `json:"prompt_version"`
	Ordinal       int    `json:"ordinal"`
	Actor         string `json:"actor"`
}

// retractFact records that a reader removed a fact (0113). The fact row is never
// touched; pages leave out retracted facts. Retracting twice is a no-op.
func (s *Store) retractFact(ctx context.Context, novelID string, req factRetractionRequest) error {
	tag, err := s.db.Exec(ctx, `INSERT INTO fact_retraction (novel_id, chapter_index, prompt_version, ordinal, retracted_by)
		SELECT novel_id, chapter_index, prompt_version, ordinal, $5 FROM chapter_fact
		 WHERE novel_id=$1 AND chapter_index=$2 AND prompt_version=$3 AND ordinal=$4
		ON CONFLICT DO NOTHING`,
		novelID, req.ChapterIndex, req.PromptVersion, req.Ordinal, req.Actor)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		var exists bool
		if err := s.db.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM chapter_fact
			WHERE novel_id=$1 AND chapter_index=$2 AND prompt_version=$3 AND ordinal=$4)`,
			novelID, req.ChapterIndex, req.PromptVersion, req.Ordinal).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return ErrFactNotFound
		}
	}
	return nil
}

func (a *API) retractFact(w http.ResponseWriter, r *http.Request) {
	var req factRetractionRequest
	if err := decodeJSONBody(r, &req); err != nil || req.ChapterIndex < 0 || req.Ordinal < 0 ||
		strings.TrimSpace(req.PromptVersion) == "" || strings.TrimSpace(req.Actor) == "" {
		writeErr(w, http.StatusBadRequest, "chapter_index, prompt_version, ordinal and actor are required")
		return
	}
	err := a.store.retractFact(r.Context(), r.PathValue("id"), req)
	switch {
	case errors.Is(err, ErrFactNotFound):
		writeErr(w, http.StatusNotFound, err.Error())
	case err != nil:
		log.Printf("retract fact: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not retract fact")
	default:
		writeJSON(w, http.StatusOK, map[string]bool{"retracted": true})
	}
}
