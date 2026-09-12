package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type recordReviewRequest struct {
	RowID     string `json:"row_id"`
	Decision  string `json:"decision"` // accepted|rejected
	Reason    string `json:"reason"`
	RequestID string `json:"request_id"`
	Actor     string `json:"actor"`
}

type recordReviewOutcome struct {
	RowID          string `json:"row_id"`
	GenerationID   string `json:"generation_id"`
	Chapter        int    `json:"chapter_index"`
	Decision       string `json:"decision"`
	AlreadyApplied bool   `json:"already_applied"`
}

func validateRecordReviewRequest(novelID string, req *recordReviewRequest) error {
	if _, err := uuid.Parse(novelID); err != nil {
		return fmt.Errorf("%w: invalid novel id", ErrRecordsReviewInvalid)
	}
	if _, err := uuid.Parse(strings.TrimSpace(req.RowID)); err != nil {
		return fmt.Errorf("%w: invalid row_id", ErrRecordsReviewInvalid)
	}
	req.RowID = strings.TrimSpace(req.RowID)
	req.Decision = strings.ToLower(strings.TrimSpace(req.Decision))
	if req.Decision != "accepted" && req.Decision != "rejected" {
		return fmt.Errorf("%w: decision must be accepted or rejected", ErrRecordsReviewInvalid)
	}
	req.Reason = strings.TrimSpace(req.Reason)
	if req.Reason == "" || len(req.Reason) > 2000 {
		return fmt.Errorf("%w: reason is required", ErrRecordsReviewInvalid)
	}
	req.RequestID = strings.TrimSpace(req.RequestID)
	if req.RequestID == "" || len(req.RequestID) > 200 {
		return fmt.Errorf("%w: request_id is required", ErrRecordsReviewInvalid)
	}
	req.Actor = strings.TrimSpace(req.Actor)
	if req.Actor == "" || len(req.Actor) > 200 {
		return fmt.Errorf("%w: actor is required", ErrRecordsReviewInvalid)
	}
	return nil
}

func recordReviewFingerprint(req recordReviewRequest) string {
	sum := sha256.Sum256([]byte(req.RowID + "\x00" + req.Decision + "\x00" + req.Reason))
	return hex.EncodeToString(sum[:])
}

func (s *Store) reviewRecord(ctx context.Context, novelID string, chapter int, req recordReviewRequest) (recordReviewOutcome, error) {
	if err := validateRecordReviewRequest(novelID, &req); err != nil {
		return recordReviewOutcome{}, err
	}
	if chapter < 0 {
		return recordReviewOutcome{}, fmt.Errorf("%w: chapter must be nonnegative", ErrRecordsReviewInvalid)
	}
	fingerprint := recordReviewFingerprint(req)
	decision := req.Decision

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return recordReviewOutcome{}, fmt.Errorf("begin record review: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return recordReviewOutcome{}, fmt.Errorf("lock record review: %w", err)
	}

	var existing recordReviewOutcome
	var existingDecision, existingFingerprint string
	err = tx.QueryRow(ctx, `SELECT row_id::text,generation_id::text,source_chapter,decision,request_fingerprint
		FROM record_review_decision WHERE novel_id=$1 AND request_id=$2`, novelID, req.RequestID).
		Scan(&existing.RowID, &existing.GenerationID, &existing.Chapter, &existingDecision, &existingFingerprint)
	if err == nil {
		if existingFingerprint != fingerprint {
			return recordReviewOutcome{}, ErrRecordsReviewConflict
		}
		existing.Decision = existingDecision
		existing.AlreadyApplied = true
		return existing, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return recordReviewOutcome{}, err
	}

	var generation string
	if err = tx.QueryRow(ctx, `SELECT g.id::text
		FROM novel n JOIN record_generation g ON g.id=n.active_record_generation
		JOIN record_row w ON w.novel_id=n.id AND w.generation_id=g.id
		JOIN record_run r ON r.id=w.run_id AND r.status='published'
		WHERE n.id=$1 AND g.state='active' AND w.id=$2 AND w.source_chapter=$3`,
		novelID, req.RowID, chapter).Scan(&generation); errors.Is(err, pgx.ErrNoRows) {
		return recordReviewOutcome{}, ErrRecordsReviewNotFound
	} else if err != nil {
		return recordReviewOutcome{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO record_review_decision
		(novel_id,generation_id,row_id,source_chapter,decision,actor,reason,request_id,request_fingerprint)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
		novelID, generation, req.RowID, chapter, decision, req.Actor, req.Reason, req.RequestID, fingerprint); err != nil {
		return recordReviewOutcome{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return recordReviewOutcome{}, fmt.Errorf("commit record review: %w", err)
	}
	return recordReviewOutcome{RowID: req.RowID, GenerationID: generation, Chapter: chapter, Decision: req.Decision}, nil
}

func (a *API) recordReview(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	var req recordReviewRequest
	if err := decodeJSONBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	result, err := a.store.reviewRecord(r.Context(), r.PathValue("id"), chapter, req)
	switch {
	case errors.Is(err, ErrRecordsReviewInvalid):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, ErrRecordsReviewNotFound), errors.Is(err, ErrRecordsReviewStale):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, ErrRecordsReviewConflict):
		writeErr(w, http.StatusConflict, err.Error())
	case err != nil:
		log.Printf("record review: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not review record")
	default:
		writeJSON(w, http.StatusOK, result)
	}
}
