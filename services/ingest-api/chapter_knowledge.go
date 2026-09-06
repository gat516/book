package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type reextractApplyBody struct {
	RevisionID  string          `json:"revision_id"`
	Version     int64           `json:"version"`
	Decisions   json.RawMessage `json:"decisions"`
	RequestedBy string          `json:"requested_by"`
}

type reextractStartBody struct {
	RequestedBy string `json:"requested_by"`
	Scope       string `json:"scope"`
}

// startChapterReextract freezes every input that can make a preview stale. The worker
// reads this run; unlike migration 0050's old job reset, nothing is published here.
func (s *Store) startChapterReextract(ctx context.Context, novelID string, chapter int, actor, scope string) (map[string]any, error) {
	if scope == "" {
		scope = "all"
	}
	if scope != "all" && scope != "terms" && scope != "facts" {
		return nil, ErrFactInvalid
	}
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return nil, err
	}
	var revision, inputHash, displayHash, modelIdentity string
	var generation, version int64
	err = tx.QueryRow(ctx, `SELECT r.id::text,c->>'source_hash',c->>'display_hash',
		COALESCE(j.model_identity,md5(r.model::text)),r.generation,r.version
	 FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision
	 JOIN LATERAL jsonb_array_elements(r.snapshot->'chapters') c ON (c->>'chapter')::int=$2
	 LEFT JOIN graph_job j ON j.revision_id=r.id AND j.chapter_index=$2
	 WHERE n.id=$1 AND r.state='active' AND r.trusted AND NOT r.legacy`, novelID, chapter).
		Scan(&revision, &inputHash, &displayHash, &modelIdentity, &generation, &version)
	if errors.Is(err, pgx.ErrNoRows) {
		// No row here means either there is no active, trusted, managed revision at all
		// (legacy or quarantined), or this chapter is not in that revision's snapshot.
		// Either way there is genuinely nothing to append to yet -- distinct from
		// ErrFactStale, which means the graph moved out from under a caller who read it
		// a moment ago.
		return nil, ErrNoManagedGraph
	}
	if err != nil {
		return nil, err
	}
	var runID string
	err = tx.QueryRow(ctx, `INSERT INTO chapter_knowledge_run
		(novel_id,chapter_index,revision_id,mode,scope,input_hash,display_hash,model_identity,graph_generation,graph_version,requested_by)
		VALUES($1,$2,$3,'reextract',$4,$5,$6,$7,$8,$9,$10) RETURNING id::text`, novelID, chapter, revision, scope, inputHash, displayHash, modelIdentity, generation, version, actor).Scan(&runID)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO chapter_knowledge_activity(run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
		VALUES($1,$2,$3,'run','run','detected','{}','run:detected')`, runID, novelID, chapter)
	if err != nil {
		return nil, err
	}
	params, _ := json.Marshal(map[string]string{"run_id": runID, "scope": scope})
	_, err = tx.Exec(ctx, `INSERT INTO repair_request(novel_id,track,action,params,requested_by,chapter_index)
		VALUES($1,'graph','reextract',$2,$3,$4)`, novelID, params, actor, chapter)
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return map[string]any{"run_id": runID, "state": "pending", "scope": scope, "revision_id": revision, "version": version}, nil
}

func (s *Store) applyChapterReextract(ctx context.Context, novelID string, chapter int, runID string, body reextractApplyBody) (map[string]any, error) {
	if !json.Valid(body.Decisions) || len(body.Decisions) == 0 {
		return nil, ErrFactInvalid
	}
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return nil, err
	}
	var revision string
	var version int64
	err = tx.QueryRow(ctx, `SELECT r.revision_id::text,r.graph_version FROM chapter_knowledge_run r
		JOIN novel n ON n.id=r.novel_id JOIN graph_revision g ON g.id=n.active_graph_revision
		WHERE r.id=$1 AND r.novel_id=$2 AND r.chapter_index=$3 AND r.state='awaiting_review'
		AND g.id=r.revision_id AND g.state='active' AND g.trusted AND g.generation=r.graph_generation AND g.version=r.graph_version
		FOR UPDATE`, runID, novelID, chapter).Scan(&revision, &version)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrFactStale
	}
	if err != nil {
		return nil, err
	}
	if body.RevisionID != revision || body.Version != version {
		return nil, ErrFactStale
	}
	params, _ := json.Marshal(map[string]any{"run_id": runID, "decisions": body.Decisions})
	_, err = tx.Exec(ctx, `INSERT INTO repair_request(novel_id,track,action,params,requested_by,chapter_index)
		VALUES($1,'graph','reextract_apply',$2,$3,$4)`, novelID, params, body.RequestedBy, chapter)
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE chapter_knowledge_run SET state='applying',updated_at=now() WHERE id=$1`, runID); err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return map[string]any{"run_id": runID, "state": "applying"}, nil
}

func (a *API) chapterKnowledgeReextract(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	body := reextractStartBody{RequestedBy: "unknown operator"}
	if r.Body != nil {
		if err = decodeJSONBody(r, &body); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON body")
			return
		}
	}
	result, err := a.store.startChapterReextract(r.Context(), r.PathValue("id"), chapter, strings.TrimSpace(body.RequestedBy), body.Scope)
	writeKnowledgeMutation(w, result, err)
}

func (a *API) chapterKnowledgeApply(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	var body reextractApplyBody
	if err = decodeJSONBody(r, &body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	result, err := a.store.applyChapterReextract(r.Context(), r.PathValue("id"), chapter, r.PathValue("run"), body)
	writeKnowledgeMutation(w, result, err)
}

func writeKnowledgeMutation(w http.ResponseWriter, result map[string]any, err error) {
	if err == nil {
		writeJSON(w, http.StatusAccepted, result)
		return
	}
	var pgErr *pgconn.PgError
	switch {
	case errors.Is(err, ErrNoManagedGraph):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrFactStale):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrFactInvalid):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.As(err, &pgErr) && pgErr.Code == "23505":
		writeErr(w, http.StatusConflict, "a chapter knowledge run is already active")
	default:
		log.Printf("chapter knowledge: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not update chapter knowledge")
	}
}
