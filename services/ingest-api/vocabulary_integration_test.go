package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"

	"github.com/google/uuid"
)

// TestVocabularyMutationsAreAppendOnlyAndVersioned exercises the complete operator
// write surface against the applied vocabulary schema. It intentionally keeps the
// fixture novel-scoped so cleanup cannot affect another book.
func TestVocabularyMutationsAreAppendOnlyAndVersioned(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	ontology := `{"kinds":["character","sect"]}`
	if _, err := store.db.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES($1,'Vocabulary integration','zh','en',$2)`, novelID, ontology); err != nil {
		t.Fatal(err)
	}
	// A terminology decision never edits published extraction: it bumps the active
	// generation's config_version, which is what rendering caches key on, and leaves
	// every record row exactly as extracted.
	var generation string
	if err := store.db.QueryRow(ctx, `SELECT active_record_generation::text FROM novel WHERE id=$1`, novelID).Scan(&generation); err != nil {
		t.Fatal(err)
	}
	entityA, entityB := uuid.NewString(), uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO entity(id,novel_id,record_generation_id,kind,canonical,first_seen_chapter) VALUES($1,$3,$4,'character','A',0),($2,$3,$4,'sect','B',0)`, entityA, entityB, novelID, generation); err != nil {
		t.Fatal(err)
	}
	runID := uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO chapter(novel_id,chapter_index,raw_uri,raw_hash,source_meta) VALUES($1,0,'raw://c','vocab-hash','{}'::jsonb)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO record_run(id,novel_id,generation_id,chapter_index,source_hash,request_identity,extraction_model,status,publication_version,published_at) VALUES($1,$2,$3,0,'vocab-hash','identity','test-model','published',1,now())`, runID, novelID, generation); err != nil {
		t.Fatal(err)
	}
	rowID := uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO record_row(id,novel_id,generation_id,run_id,original_index,record_type,source_chapter,source_hash) VALUES($1,$2,$3,$4,0,'EVENT',0,'vocab-hash')`, rowID, novelID, generation, runID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO record_value(row_id,field_name,source_value) VALUES($1,'what','sentinel')`, rowID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,first_seen_chapter,admitted_at_chapter) VALUES($1,'attribute','candidate_term',ARRAY['character'],'candidate',0,NULL)`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, q := range []string{`DELETE FROM novel_vocabulary_changelog WHERE novel_id=$1`, `DELETE FROM novel_vocabulary_alias WHERE novel_id=$1`, `DELETE FROM novel_vocabulary_chapter WHERE novel_id=$1`, `DELETE FROM novel_vocabulary WHERE novel_id=$1`, `DELETE FROM novel WHERE id=$1`} {
			_, _ = store.db.Exec(context.Background(), q, novelID)
		}
	})
	configVersion := func() int64 {
		var v int64
		if err := store.db.QueryRow(ctx, `SELECT config_version FROM record_generation WHERE id=$1`, generation).Scan(&v); err != nil {
			t.Fatal(err)
		}
		return v
	}
	before := configVersion()
	mut := func(req vocabularyMutationRequest) {
		req.CreatedBy = "test"
		if _, err := store.MutateVocabulary(ctx, novelID, req); err != nil {
			t.Fatalf("%s: %v", req.Action, err)
		}
	}
	mut(vocabularyMutationRequest{Action: "edit-gloss", TermType: "attribute", Name: "description", Gloss: "durable", Chapter: 0})
	mut(vocabularyMutationRequest{Action: "set-cardinality", TermType: "attribute", Name: "description", Cardinality: "single", Chapter: 0})
	mut(vocabularyMutationRequest{Action: "set-kinds", TermType: "attribute", Name: "description", Kinds: []string{"character"}, Chapter: 0})
	mut(vocabularyMutationRequest{Action: "set-kinds", TermType: "relation", Name: "ally", Kinds: []string{"character"}, DstKinds: []string{"sect"}, Chapter: 0})
	if _, err := store.MutateVocabulary(ctx, novelID, vocabularyMutationRequest{Action: "set-kinds", TermType: "attribute", Name: "description", Kinds: []string{"unknown"}, Chapter: 0}); !errors.Is(err, ErrVocabularyInvalid) {
		t.Fatalf("invalid kind err=%v", err)
	}
	mut(vocabularyMutationRequest{Action: "rename-to-alias", TermType: "attribute", Name: "description", Alias: "old_description", Chapter: 0, CreatedBy: "test"})
	mut(vocabularyMutationRequest{Action: "ban", TermType: "attribute", Name: "description", Chapter: 0})
	if _, err := store.MutateVocabulary(ctx, novelID, vocabularyMutationRequest{Action: "admit", TermType: "attribute", Name: "description", Chapter: 0}); !errors.Is(err, ErrVocabularyConflict) {
		t.Fatalf("banned admit err=%v", err)
	}
	mut(vocabularyMutationRequest{Action: "admit", TermType: "attribute", Name: "candidate_term", Chapter: 1})
	if got := configVersion() - before; got != 7 {
		t.Fatalf("config_version advanced by %d, want 7", got)
	}
	var field, value string
	if err := store.db.QueryRow(ctx, `SELECT field_name,source_value FROM record_value WHERE row_id=$1`, rowID).Scan(&field, &value); err != nil {
		t.Fatal(err)
	}
	if field != "what" || value != "sentinel" {
		t.Fatalf("published record changed: %q %q", field, value)
	}
	var alias string
	if err := store.db.QueryRow(ctx, `SELECT surface FROM novel_vocabulary_alias WHERE novel_id=$1`, novelID).Scan(&alias); err != nil {
		t.Fatal(err)
	}
	if alias != "old_description" {
		t.Fatalf("alias=%q", alias)
	}
	rows, err := store.db.Query(ctx, `SELECT seq,term_type,name,action,changed_at_chapter,COALESCE(prev_hash,''),row_hash FROM novel_vocabulary_changelog WHERE novel_id=$1 ORDER BY seq`, novelID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	prev := ""
	for rows.Next() {
		var seq, chapter int
		var typ, name, action, prevHash, rowHash string
		if err := rows.Scan(&seq, &typ, &name, &action, &chapter, &prevHash, &rowHash); err != nil {
			t.Fatal(err)
		}
		payload, _ := json.Marshal([]any{novelID, seq, typ, name, action, chapter, prevHash})
		sum := sha256.Sum256(append([]byte(prevHash), payload...))
		if hex.EncodeToString(sum[:]) != rowHash || prevHash != prev {
			t.Fatalf("broken hash seq %d", seq)
		}
		prev = rowHash
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
}
