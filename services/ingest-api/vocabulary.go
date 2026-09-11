package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
)

type vocabularyMutationRequest struct {
	Action      string   `json:"action"`
	TermType    string   `json:"term_type"`
	Name        string   `json:"name"`
	Alias       string   `json:"alias,omitempty"`
	Chapter     int      `json:"chapter"`
	Cardinality string   `json:"cardinality,omitempty"`
	Kinds       []string `json:"kinds,omitempty"`
	DstKinds    []string `json:"dst_kinds,omitempty"`
	Gloss       string   `json:"gloss,omitempty"`
	CreatedBy   string   `json:"created_by,omitempty"`
}

var vocabularyNameRE = regexp.MustCompile(`^[a-z][a-z0-9_]{1,39}$`)

func validateVocabularyName(name string) error {
	if !vocabularyNameRE.MatchString(name) {
		return errors.New("name must be normalized snake_case (2-40 ASCII characters)")
	}
	return nil
}

func validateVocabularyKinds(kinds []string, allowed map[string]bool) error {
	seen := map[string]bool{}
	for _, kind := range kinds {
		if !vocabularyNameRE.MatchString(kind) || seen[kind] {
			return fmt.Errorf("invalid or duplicate kind %q", kind)
		}
		if len(allowed) > 0 && !allowed[kind] {
			return fmt.Errorf("kind %q is not in the novel ontology", kind)
		}
		seen[kind] = true
	}
	return nil
}

func ontologyKinds(raw []byte) map[string]bool {
	var ontology struct {
		Kinds []string `json:"kinds"`
	}
	_ = json.Unmarshal(raw, &ontology)
	allowed := make(map[string]bool, len(ontology.Kinds))
	for _, kind := range ontology.Kinds {
		allowed[kind] = true
	}
	return allowed
}

func (a *API) mutateVocabulary(w http.ResponseWriter, r *http.Request) {
	var req vocabularyMutationRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.CreatedBy == "" {
		req.CreatedBy = "operator"
	}
	version, err := a.store.MutateVocabulary(r.Context(), r.PathValue("id"), req)
	if errors.Is(err, ErrVocabularyInvalid) {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if errors.Is(err, ErrVocabularyNotFound) {
		writeErr(w, http.StatusNotFound, "no such vocabulary term")
		return
	}
	if errors.Is(err, ErrVocabularyConflict) {
		writeErr(w, http.StatusConflict, err.Error())
		return
	}
	if err != nil {
		log.Printf("mutate vocabulary: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not update vocabulary")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"novel_id": r.PathValue("id"), "name": req.Name, "version": version})
}

var (
	ErrVocabularyInvalid  = errors.New("invalid vocabulary mutation")
	ErrVocabularyNotFound = errors.New("vocabulary term not found")
	ErrVocabularyConflict = errors.New("vocabulary mutation conflicts with existing state")
)

func validateVocabularyMutation(req vocabularyMutationRequest) error {
	if req.TermType != "attribute" && req.TermType != "relation" {
		return fmt.Errorf("%w: term_type must be attribute or relation", ErrVocabularyInvalid)
	}
	if err := validateVocabularyName(req.Name); err != nil {
		return fmt.Errorf("%w: %v", ErrVocabularyInvalid, err)
	}
	if req.Chapter < 0 {
		return fmt.Errorf("%w: chapter must be nonnegative", ErrVocabularyInvalid)
	}
	switch req.Action {
	case "admit", "ban", "rename-to-alias", "set-cardinality", "set-kinds", "edit-gloss":
	default:
		return fmt.Errorf("%w: unsupported action", ErrVocabularyInvalid)
	}
	if req.Action == "rename-to-alias" {
		if err := validateVocabularyName(req.Alias); err != nil {
			return fmt.Errorf("%w: alias: %v", ErrVocabularyInvalid, err)
		}
		if req.Alias == req.Name {
			return fmt.Errorf("%w: alias must differ from canonical name", ErrVocabularyInvalid)
		}
	} else if req.Alias != "" {
		return fmt.Errorf("%w: alias only applies to rename-to-alias", ErrVocabularyInvalid)
	}
	if req.Action == "set-cardinality" && req.Cardinality != "single" && req.Cardinality != "accretive" {
		return fmt.Errorf("%w: cardinality must be single or accretive", ErrVocabularyInvalid)
	}
	if req.TermType == "relation" && req.Action == "set-cardinality" {
		return fmt.Errorf("%w: relations are accretive", ErrVocabularyInvalid)
	}
	if req.Action == "set-kinds" && req.TermType == "relation" && len(req.DstKinds) == 0 {
		return fmt.Errorf("%w: relation dst_kinds required", ErrVocabularyInvalid)
	}
	return nil
}

func canonicalVocabularyAction(action string) string {
	switch action {
	case "rename-to-alias":
		return "alias"
	case "set-cardinality":
		return "cardinality"
	case "set-kinds":
		return "kinds"
	case "edit-gloss":
		return "gloss"
	default:
		return action
	}
}

func sortedStrings(values []string) []string {
	out := append([]string{}, values...)
	sort.Strings(out)
	return out
}

// stableVocabularyPayload is compact JSON with deterministic arrays, suitable for the
// same sha256(prev_hash + payload) audit convention as glossary_changelog.
func stableVocabularyPayload(novelID string, seq int, req vocabularyMutationRequest, oldValue, newValue any, prev string) ([]byte, error) {
	// Keep this exact seven-field array in lockstep with pipeline.vocabulary._admission_changelog:
	// [novel_id, seq, term_type, name, action, chapter, prev_hash]. The database retains
	// old/new values for audit detail, but they are deliberately not part of the chain
	// identity so automatic admission and operator edits share one cross-language ledger.
	marshal := func(value any) ([]byte, error) {
		var b strings.Builder
		enc := json.NewEncoder(&b)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(value); err != nil {
			return nil, err
		}
		return []byte(strings.TrimSuffix(b.String(), "\n")), nil
	}
	_ = oldValue
	_ = newValue
	return marshal([]any{novelID, seq, req.TermType, req.Name, req.Action, req.Chapter, prev})
}

func (s *Store) MutateVocabulary(ctx context.Context, novelID string, req vocabularyMutationRequest) (int64, error) {
	if err := validateVocabularyMutation(req); err != nil {
		return 0, err
	}
	auditAction := canonicalVocabularyAction(req.Action)
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return 0, err
	}
	var ontology []byte
	if err = tx.QueryRow(ctx, "SELECT ontology FROM novel WHERE id=$1 FOR UPDATE", novelID).Scan(&ontology); errors.Is(err, pgx.ErrNoRows) {
		return 0, ErrVocabularyNotFound
	}
	if err != nil {
		return 0, err
	}
	allowed := ontologyKinds(ontology)
	if err := validateVocabularyKinds(req.Kinds, allowed); err != nil {
		return 0, fmt.Errorf("%w: %v", ErrVocabularyInvalid, err)
	}
	if err := validateVocabularyKinds(req.DstKinds, allowed); err != nil {
		return 0, fmt.Errorf("%w: %v", ErrVocabularyInvalid, err)
	}
	var oldName, oldStatus, oldCard, oldGloss string
	var oldKinds, oldDst []string
	var oldVersion int64
	err = tx.QueryRow(ctx, `SELECT name,status,cardinality,kinds,dst_kinds,gloss,version FROM novel_vocabulary WHERE novel_id=$1 AND term_type=$2 AND name=$3 FOR UPDATE`, novelID, req.TermType, req.Name).Scan(&oldName, &oldStatus, &oldCard, &oldKinds, &oldDst, &oldGloss, &oldVersion)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, ErrVocabularyNotFound
	}
	if err != nil {
		return 0, err
	}
	if req.Action == "rename-to-alias" {
		var effective string
		if err := tx.QueryRow(ctx, `SELECT canonical_novel_vocabulary_name($1,$2,$3,$4)`, novelID, req.TermType, req.Alias, req.Chapter).Scan(&effective); err != nil {
			return 0, err
		}
		if effective == req.Name {
			return oldVersion, nil
		}
	}
	if req.Action == "ban" && oldStatus == "banned" {
		return oldVersion, nil
	}
	if req.Action == "admit" && oldStatus == "banned" {
		return 0, ErrVocabularyConflict
	}
	newStatus, newCard, newKinds, newDst, newGloss := oldStatus, oldCard, oldKinds, oldDst, oldGloss
	var admitted any = nil
	switch req.Action {
	case "admit":
		newStatus, admitted = "admitted", req.Chapter
	case "ban":
		newStatus = "banned"
	case "set-cardinality":
		newCard = req.Cardinality
	case "set-kinds":
		newKinds, newDst = sortedStrings(req.Kinds), sortedStrings(req.DstKinds)
	case "edit-gloss":
		newGloss = strings.TrimSpace(req.Gloss)
	case "rename-to-alias":
		if _, err = tx.Exec(ctx, `INSERT INTO novel_vocabulary_alias(novel_id,term_type,surface,name,known_from_chapter,created_by) VALUES($1,$2,$3,$4,$5,$6)`, novelID, req.TermType, req.Alias, req.Name, req.Chapter, req.CreatedBy); err != nil {
			return 0, err
		}
	}
	newVersion := oldVersion + 1
	if _, err = tx.Exec(ctx, `UPDATE novel_vocabulary SET status=$4,cardinality=$5,kinds=$6,dst_kinds=$7,gloss=$8,admitted_at_chapter=CASE WHEN $4='admitted' THEN COALESCE(admitted_at_chapter,$9) ELSE admitted_at_chapter END,version=$10,updated_at=now() WHERE novel_id=$1 AND term_type=$2 AND name=$3`, novelID, req.TermType, req.Name, newStatus, newCard, newKinds, newDst, newGloss, admitted, newVersion); err != nil {
		return 0, err
	}
	var seq int
	var prev string
	err = tx.QueryRow(ctx, `SELECT seq,row_hash FROM novel_vocabulary_changelog WHERE novel_id=$1 ORDER BY seq DESC LIMIT 1`, novelID).Scan(&seq, &prev)
	if errors.Is(err, pgx.ErrNoRows) {
		seq, prev = 0, ""
	} else if err != nil {
		return 0, err
	}
	seq++
	oldValue := map[string]any{"status": oldStatus, "cardinality": oldCard, "kinds": oldKinds, "dst_kinds": oldDst, "gloss": oldGloss}
	newValue := map[string]any{"status": newStatus, "cardinality": newCard, "kinds": newKinds, "dst_kinds": newDst, "gloss": newGloss}
	hashReq := req
	hashReq.Action = auditAction
	payload, err := stableVocabularyPayload(novelID, seq, hashReq, oldValue, newValue, prev)
	if err != nil {
		return 0, err
	}
	sum := sha256.Sum256(append([]byte(prev), payload...))
	rowHash := hex.EncodeToString(sum[:])
	if _, err = tx.Exec(ctx, `INSERT INTO novel_vocabulary_changelog(novel_id,seq,term_type,action,name,old_name,new_name,changed_at_chapter,old_value,new_value,prev_hash,row_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, novelID, seq, req.TermType, auditAction, req.Name, oldName, func() any {
		if req.Alias != "" {
			return req.Alias
		}
		return nil
	}(), req.Chapter, oldValue, newValue, func() any {
		if prev != "" {
			return prev
		}
		return nil
	}(), rowHash, req.CreatedBy); err != nil {
		return 0, err
	}
	// Terminology changes invalidate future renderings while leaving published source
	// records immutable. The generation config version is included in rendering cache
	// keys by the records worker.
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET config_version=config_version+1 WHERE novel_id=$1 AND id=(SELECT active_record_generation FROM novel WHERE id=$1)`, novelID); err != nil {
		return 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, err
	}
	return newVersion, nil
}
