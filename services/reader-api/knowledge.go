package main

import (
	"context"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
)

type KnowledgeStatus struct {
	RevisionID string `json:"revision_id"`
	Version    int64  `json:"version"`
	Trusted    bool   `json:"trusted"`
	Status     string `json:"status"`
}

func knowledgeInTx(ctx context.Context, tx pgx.Tx, chapter int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := tx.QueryRow(ctx, `SELECT revision_id::text,version,trusted,status FROM reader_knowledge_status($1)`, chapter).
		Scan(&k.RevisionID, &k.Version, &k.Trusted, &k.Status)
	return k, err
}

func (s *Store) KnowledgeStatus(ctx context.Context, novel string, chapter, at int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := s.withReaderTx(ctx, novel, at, func(tx pgx.Tx) error {
		var err error
		k, err = knowledgeInTx(ctx, tx, chapter)
		return err
	})
	return k, err
}

func (a *API) getKnowledgeStatus(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.URL.Query().Get("chapter"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	k, err := a.store.KnowledgeStatus(r.Context(), novel, chapter, at)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, k)
}

// Generation changes during a multi-query graph request invalidate the entire response.
func (a *API) knowledgeUnchanged(w http.ResponseWriter, r *http.Request, novel string, at int, before KnowledgeStatus) bool {
	after, err := a.store.KnowledgeStatus(r.Context(), novel, at, at)
	if err != nil || after.RevisionID != before.RevisionID || after.Version != before.Version {
		writeError(w, http.StatusConflict, "knowledge changed; retry request")
		return false
	}
	return true
}
