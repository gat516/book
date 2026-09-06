package main

// Human fact edits. Semantic edits append a successor; only value_en is mutable because
// it is display text rather than source evidence (instructions.md §0.2, migration 0051).

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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
	ErrFactNotFound = errors.New("no such active fact")
	ErrFactStale    = errors.New("knowledge changed; reload before editing")
	ErrFactInvalid  = errors.New("invalid fact edit")
)

type factEditRequest struct {
	RevisionID string `json:"revision_id"`
	Version    int64  `json:"version"`
	ValueEN    string `json:"value_en"`
	Attribute  string `json:"attribute"`
	Note       string `json:"note"`
	Actor      string `json:"actor"`
}

type factEditResponse struct {
	FactID      int64  `json:"fact_id"`
	SuccessorID *int64 `json:"successor_fact_id,omitempty"`
	RevisionID  string `json:"revision_id"`
	Version     int64  `json:"version"`
	Action      string `json:"action"`
}

type lockedFact struct {
	id, entityID             string
	attribute, value         string
	valueEN                  *string
	validFrom, sourceChapter int
	confidence               float32
	evidenceID               *string
	generation               int64
}

func factClaimKey(action string, factID int64, attribute, valueEN string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("human\x1f%s\x1f%d\x1f%s\x1f%s", action, factID, attribute, valueEN)))
	return hex.EncodeToString(sum[:])
}

func (s *Store) editFact(ctx context.Context, novelID string, factID int64, action string, body factEditRequest) (factEditResponse, error) {
	if _, err := uuid.Parse(novelID); err != nil || factID <= 0 {
		return factEditResponse{}, ErrFactInvalid
	}
	if _, err := uuid.Parse(body.RevisionID); err != nil || body.Version <= 0 {
		return factEditResponse{}, fmt.Errorf("%w: revision_id and version are required", ErrFactInvalid)
	}
	body.ValueEN = strings.TrimSpace(body.ValueEN)
	body.Attribute = strings.TrimSpace(body.Attribute)
	body.Actor = strings.TrimSpace(body.Actor)
	body.Note = strings.TrimSpace(body.Note)
	if body.Actor == "" || len(body.Actor) > 200 || len(body.ValueEN) > 200 || len(body.Note) > 2000 {
		return factEditResponse{}, ErrFactInvalid
	}
	if action != "retraction" && body.ValueEN == "" {
		return factEditResponse{}, fmt.Errorf("%w: value_en is required", ErrFactInvalid)
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return factEditResponse{}, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return factEditResponse{}, err
	}

	var currentRevision string
	var currentVersion, generation int64
	var trusted, legacy bool
	err = tx.QueryRow(ctx, `SELECT r.id::text,r.version,r.generation,r.trusted,r.legacy
		FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision WHERE n.id=$1 FOR UPDATE`, novelID).
		Scan(&currentRevision, &currentVersion, &generation, &trusted, &legacy)
	if errors.Is(err, pgx.ErrNoRows) {
		return factEditResponse{}, ErrFactNotFound
	}
	if err != nil {
		return factEditResponse{}, err
	}
	if currentRevision != body.RevisionID || currentVersion != body.Version || !trusted || legacy {
		return factEditResponse{}, ErrFactStale
	}

	var f lockedFact
	err = tx.QueryRow(ctx, `SELECT f.id::text,f.entity_id::text,f.attribute,f.value,f.value_en,
		f.valid_from_chapter,f.source_chapter,f.confidence,f.evidence_id::text
		FROM fact f WHERE f.id=$1 AND f.novel_id=$2 AND f.revision_id=$3
		AND f.kind<>'retraction'
		AND NOT EXISTS (SELECT 1 FROM fact s WHERE s.revision_id=f.revision_id AND s.supersedes=f.id)
		FOR UPDATE`, factID, novelID, currentRevision).Scan(&f.id, &f.entityID, &f.attribute, &f.value, &f.valueEN,
		&f.validFrom, &f.sourceChapter, &f.confidence, &f.evidenceID)
	if errors.Is(err, pgx.ErrNoRows) {
		return factEditResponse{}, ErrFactNotFound
	}
	if err != nil {
		return factEditResponse{}, err
	}
	if f.evidenceID == nil {
		return factEditResponse{}, fmt.Errorf("%w: fact has no source evidence", ErrFactInvalid)
	}

	attribute := f.attribute
	if action == "correction" {
		attribute = body.Attribute
		if attribute == "" {
			return factEditResponse{}, fmt.Errorf("%w: attribute is required", ErrFactInvalid)
		}
		var allowed bool
		err = tx.QueryRow(ctx, `SELECT EXISTS(
			SELECT 1 FROM novel n JOIN entity e ON e.id=$2 AND e.novel_id=n.id,
			LATERAL jsonb_array_elements(COALESCE(n.ontology->'attributes','[]')) a
			WHERE n.id=$1 AND CASE WHEN jsonb_typeof(a)='string' THEN a#>>'{}' ELSE a->>'name' END=$3 AND
			  (jsonb_typeof(a)='string' OR jsonb_typeof(a->'kinds')<>'array' OR a->'kinds' ? e.kind))`, novelID, f.entityID, attribute).Scan(&allowed)
		if err != nil {
			return factEditResponse{}, err
		}
		if !allowed {
			return factEditResponse{}, fmt.Errorf("%w: attribute is not allowed for this entity kind", ErrFactInvalid)
		}
	}

	if _, err = tx.Exec(ctx, "SELECT set_config('app.graph_revision',$1,true),set_config('app.graph_generation',$2,true)", currentRevision, fmt.Sprint(generation)); err != nil {
		return factEditResponse{}, err
	}
	oldDisplay := f.value
	if f.valueEN != nil {
		oldDisplay = *f.valueEN
	}
	result := factEditResponse{FactID: factID, RevisionID: currentRevision, Action: action}
	if action == "display" {
		if _, err = tx.Exec(ctx, `UPDATE fact SET value_en=$1 WHERE id=$2`, body.ValueEN, factID); err != nil {
			return factEditResponse{}, err
		}
	} else {
		kind := "correction"
		newDisplay := body.ValueEN
		if action == "retraction" {
			kind = "retraction"
			newDisplay = ""
		}
		var successor int64
		err = tx.QueryRow(ctx, `INSERT INTO fact
			(novel_id,entity_id,attribute,value,value_en,valid_from_chapter,source_chapter,
			 confidence,kind,supersedes,revision_id,evidence_id,claim_key)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id`,
			novelID, f.entityID, attribute, f.value, nilIfEmpty(newDisplay), f.validFrom, f.sourceChapter,
			f.confidence, kind, factID, currentRevision, *f.evidenceID, factClaimKey(action, factID, attribute, newDisplay)).Scan(&successor)
		if err != nil {
			return factEditResponse{}, err
		}
		result.SuccessorID = &successor
	}
	var successor any
	if result.SuccessorID != nil {
		successor = *result.SuccessorID
	}
	newDisplay := body.ValueEN
	if action == "retraction" {
		newDisplay = ""
	}
	_, err = tx.Exec(ctx, `INSERT INTO fact_edit_audit
		(novel_id,revision_id,fact_id,action,actor,old_display,new_display,successor_fact_id,note)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, novelID, currentRevision, factID, action, body.Actor,
		oldDisplay, nilIfEmpty(newDisplay), successor, nilIfEmpty(body.Note))
	if err != nil {
		return factEditResponse{}, err
	}
	err = tx.QueryRow(ctx, `UPDATE graph_revision SET version=version+1,review=NULL WHERE id=$1 RETURNING version`, currentRevision).Scan(&result.Version)
	if err != nil {
		return factEditResponse{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return factEditResponse{}, err
	}
	return result, nil
}

func nilIfEmpty(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// Keep this helper close to the write contract: handlers add the authenticated reader as
// actor and never accept a browser-supplied audit identity.
func encodeFactActor(body json.RawMessage, actor string) (json.RawMessage, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil {
		return nil, err
	}
	encoded, _ := json.Marshal(actor)
	object["actor"] = encoded
	return json.Marshal(object)
}

func (a *API) mutateFact(w http.ResponseWriter, r *http.Request) {
	factID, err := strconv.ParseInt(r.PathValue("fact"), 10, 64)
	if err != nil || factID <= 0 {
		writeErr(w, http.StatusBadRequest, "invalid fact id")
		return
	}
	var body factEditRequest
	if err = decodeJSONBody(r, &body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	action := "display"
	if r.Method == http.MethodDelete {
		action = "retraction"
	} else if strings.HasSuffix(r.URL.Path, "/corrections") {
		action = "correction"
	}
	result, err := a.store.editFact(r.Context(), r.PathValue("id"), factID, action, body)
	switch {
	case errors.Is(err, ErrFactNotFound):
		writeErr(w, http.StatusNotFound, err.Error())
	case errors.Is(err, ErrFactStale):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrFactInvalid):
		writeErr(w, http.StatusBadRequest, err.Error())
	case err != nil:
		log.Printf("edit fact: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not edit fact")
	default:
		writeJSON(w, http.StatusOK, result)
	}
}
