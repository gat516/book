package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
)

var ErrNameReviewNotFound = errors.New("character-name review not found")

type approveCharacterNameReq struct {
	TargetTerm string `json:"target_term"`
	TermRole   string `json:"term_role"`
	Reviewer   string `json:"reviewer,omitempty"`
}

func renderingForRole(role string) (constraintClass, method string, ok bool) {
	switch role {
	case "chinese_person":
		return "character_name", "pinyin", true
	case "foreign_person":
		return "character_name", "restored_name", true
	case "personal_title":
		return "character_name", "translated_title", true
	case "semantic_term":
		return "semantic_term", "semantic_translation", true
	default:
		return "", "", false
	}
}

// ApproveCharacterName atomically publishes the rendering decision and the corresponding
// glossary constraint (hard spelling for people/titles, semantic for other terms). It
// returns chapters now eligible to resume; completed chapters become replacements so
// their current version remains readable.
func (s *Store) ApproveCharacterName(ctx context.Context, novelID, sourceTerm, targetTerm, termRole, reviewer string) (int, []QueueMessage, error) {
	if problem := sourceTermProblem(sourceTerm); problem != "" {
		return 0, nil, fmt.Errorf("%w: %s", ErrGlossaryTermInvalid, problem)
	}
	targetTerm = strings.TrimSpace(targetTerm)
	if targetTerm == "" || len([]rune(targetTerm)) > 160 {
		return 0, nil, fmt.Errorf("target_term is required and must be at most 160 characters")
	}
	if reviewer == "" {
		reviewer = "human"
	}
	constraintClass, renderingMethod, ok := renderingForRole(termRole)
	if !ok {
		return 0, nil, fmt.Errorf("term_role must be chinese_person, foreign_person, personal_title, or semantic_term")
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, nil, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return 0, nil, err
	}

	var status string
	var selected *string
	if err = tx.QueryRow(ctx, `SELECT status, selected_target FROM character_name_review
		WHERE novel_id=$1 AND source_term=$2 FOR UPDATE`, novelID, sourceTerm).Scan(&status, &selected); errors.Is(err, pgx.ErrNoRows) {
		return 0, nil, ErrNameReviewNotFound
	} else if err != nil {
		return 0, nil, err
	}
	if status == "approved" {
		if selected == nil || *selected != targetTerm {
			return 0, nil, fmt.Errorf("%w: %q is already approved as %q", ErrGlossaryTermConflict, sourceTerm, valueOrEmpty(selected))
		}
		var version int
		var approvedClass string
		if err := tx.QueryRow(ctx, `SELECT version,constraint_class FROM glossary WHERE novel_id=$1 AND source_term=$2 AND NOT deleted`, novelID, sourceTerm).Scan(&version, &approvedClass); err != nil {
			return 0, nil, err
		}
		if approvedClass != constraintClass {
			return 0, nil, fmt.Errorf("%w: %q was approved with a different term role", ErrGlossaryTermConflict, sourceTerm)
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, nil, err
		}
		return version, nil, nil
	}

	var maxVersion int
	if err := tx.QueryRow(ctx, `SELECT COALESCE(MAX(version),0) FROM glossary WHERE novel_id=$1`, novelID).Scan(&maxVersion); err != nil {
		return 0, nil, err
	}
	version := maxVersion + 1
	var existingTarget, existingClass string
	var existingVersion int
	var existingDeleted bool
	err = tx.QueryRow(ctx, `SELECT target_term, constraint_class, version, deleted FROM glossary
		WHERE novel_id=$1 AND source_term=$2`, novelID, sourceTerm).Scan(&existingTarget, &existingClass, &existingVersion, &existingDeleted)
	newRow := errors.Is(err, pgx.ErrNoRows)
	if err != nil && !newRow {
		return 0, nil, err
	}
	if !newRow && !existingDeleted && existingTarget != targetTerm {
		return 0, nil, fmt.Errorf("%w: %q is already locked to %q", ErrGlossaryTermConflict, sourceTerm, existingTarget)
	}
	publishNew := newRow || existingDeleted || existingClass != constraintClass
	if publishNew {
		if newRow {
			_, err = tx.Exec(ctx, `INSERT INTO glossary
			(novel_id,source_term,target_term,version,locked_at_chapter,constraint_class)
			VALUES($1,$2,$3,$4,0,$5)`, novelID, sourceTerm, targetTerm, version, constraintClass)
		} else {
			_, err = tx.Exec(ctx, `UPDATE glossary SET target_term=$3,version=$4,locked_at_chapter=0,
				constraint_class=$5,deleted=false,entity_id=NULL
				WHERE novel_id=$1 AND source_term=$2`, novelID, sourceTerm, targetTerm, version, constraintClass)
		}
		if err != nil {
			return 0, nil, err
		}
		var prevSeq int
		var prevHash string
		err = tx.QueryRow(ctx, `SELECT seq,row_hash FROM glossary_changelog WHERE novel_id=$1 ORDER BY seq DESC LIMIT 1`, novelID).Scan(&prevSeq, &prevHash)
		if errors.Is(err, pgx.ErrNoRows) {
			prevSeq, prevHash, err = 0, "", nil
		}
		if err != nil {
			return 0, nil, err
		}
		seq := prevSeq + 1
		oldTarget := ""
		if !newRow && !existingDeleted {
			oldTarget = existingTarget
		}
		payload := pythonJSONArray(novelID, seq, sourceTerm, oldTarget, targetTerm, 0, prevHash)
		sum := sha256.Sum256([]byte(prevHash + payload))
		var prevArg any
		if prevHash != "" {
			prevArg = prevHash
		}
		var oldTargetArg any
		if oldTarget != "" {
			oldTargetArg = oldTarget
		}
		if _, err = tx.Exec(ctx, `INSERT INTO glossary_changelog
			(novel_id,seq,source_term,old_target,new_target,changed_at_chapter,prev_hash,row_hash)
			VALUES($1,$2,$3,$4,$5,0,$6,$7)`, novelID, seq, sourceTerm, oldTargetArg, targetTerm, prevArg, hex.EncodeToString(sum[:])); err != nil {
			return 0, nil, err
		}
	} else {
		version = existingVersion
	}
	if _, err = tx.Exec(ctx, `UPDATE character_name_review SET status='approved',selected_target=$3,
		selection_source=CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(candidates) c
			WHERE c->>'target_term'=$3) THEN 'offered' ELSE 'override' END,
		term_role=$4,rendering_method=$5,
		reviewed_by=$6,reviewed_at=now(),updated_at=now() WHERE novel_id=$1 AND source_term=$2`,
		novelID, sourceTerm, targetTerm, termRole, renderingMethod, reviewer); err != nil {
		return 0, nil, err
	}

	rows, err := tx.Query(ctx, `SELECT DISTINCT c.chapter_index,c.translation_ready
		FROM character_name_occurrence o JOIN chapter c ON c.novel_id=o.novel_id AND c.chapter_index=o.chapter_index
		WHERE o.novel_id=$1 AND o.source_term=$2 AND (
			c.translation_ready OR (c.status='needs_name_review' AND NOT EXISTS (
				SELECT 1 FROM character_name_occurrence o2 JOIN character_name_review r2
				ON r2.novel_id=o2.novel_id AND r2.source_term=o2.source_term
				WHERE o2.novel_id=c.novel_id AND o2.chapter_index=c.chapter_index AND r2.status='pending'
			))) ORDER BY c.chapter_index`, novelID, sourceTerm)
	if err != nil {
		return 0, nil, err
	}
	var queue []QueueMessage
	for rows.Next() {
		var chapter int
		var ready bool
		if err := rows.Scan(&chapter, &ready); err != nil {
			rows.Close()
			return 0, nil, err
		}
		queue = append(queue, QueueMessage{NovelID: novelID, ChapterIndex: chapter, Retranslate: ready})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, nil, err
	}
	for _, msg := range queue {
		if !msg.Retranslate {
			if _, err = tx.Exec(ctx, `UPDATE chapter SET status='queued' WHERE novel_id=$1 AND chapter_index=$2`, novelID, msg.ChapterIndex); err != nil {
				return 0, nil, err
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, nil, err
	}
	for _, msg := range queue {
		if err := s.enqueue(ctx, msg); err != nil {
			return version, queue, err
		}
	}
	return version, queue, nil
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func (a *API) approveCharacterName(w http.ResponseWriter, r *http.Request) {
	var req approveCharacterNameReq
	if err := decodeJSONBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	req.TargetTerm = strings.TrimSpace(req.TargetTerm)
	version, queued, err := a.store.ApproveCharacterName(r.Context(), r.PathValue("id"), r.PathValue("term"), req.TargetTerm, req.TermRole, req.Reviewer)
	if errors.Is(err, ErrNameReviewNotFound) {
		writeErr(w, http.StatusNotFound, err.Error())
		return
	}
	if errors.Is(err, ErrGlossaryTermConflict) {
		writeErr(w, http.StatusConflict, err.Error())
		return
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"source_term": r.PathValue("term"), "target_term": req.TargetTerm, "term_role": req.TermRole, "version": version, "queued_chapters": queued})
}

func decodeJSONBody(r *http.Request, value any) error {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	return decoder.Decode(value)
}
