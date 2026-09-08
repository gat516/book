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
	var legacy string
	if err := store.db.QueryRow(ctx, `SELECT active_graph_revision::text FROM novel WHERE id=$1`, novelID).Scan(&legacy); err != nil {
		t.Fatal(err)
	}
	var staging string
	if err := store.db.QueryRow(ctx, `INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology,snapshot) VALUES($1,'staging',false,false,$2,'{}') RETURNING id::text`, novelID, ontology).Scan(&staging); err != nil {
		t.Fatal(err)
	}
	entityA, entityB := uuid.NewString(), uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter) VALUES($1,$3,'character','A',0),($2,$3,'sect','B',0)`, entityA, entityB, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO fact(novel_id,entity_id,attribute,value,valid_from_chapter,source_chapter) VALUES($1,$2,'description','sentinel',0,0)`, novelID, entityA); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO edge(novel_id,src_id,dst_id,rel_type,valid_from_chapter,source_chapter) VALUES($1,$2,$3,'ally',0,0)`, novelID, entityA, entityB); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,first_seen_chapter,admitted_at_chapter) VALUES($1,'attribute','candidate_term',ARRAY['character'],'candidate',0,NULL)`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, q := range []string{`DELETE FROM novel_vocabulary_changelog WHERE novel_id=$1`, `DELETE FROM novel_vocabulary_alias WHERE novel_id=$1`, `DELETE FROM novel_vocabulary_chapter WHERE novel_id=$1`, `DELETE FROM novel_vocabulary WHERE novel_id=$1`, `DELETE FROM edge WHERE novel_id=$1`, `DELETE FROM fact WHERE novel_id=$1`, `DELETE FROM entity WHERE novel_id=$1`, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, `DELETE FROM graph_revision WHERE novel_id=$1`, `DELETE FROM novel WHERE id=$1`} {
			_, _ = store.db.Exec(context.Background(), q, novelID)
		}
	})
	versions := func() (int64, int64) {
		var a, b int64
		if err := store.db.QueryRow(ctx, `SELECT version FROM graph_revision WHERE id=$1`, legacy).Scan(&a); err != nil {
			t.Fatal(err)
		}
		if err := store.db.QueryRow(ctx, `SELECT version FROM graph_revision WHERE id=$1`, staging).Scan(&b); err != nil {
			t.Fatal(err)
		}
		return a, b
	}
	a0, b0 := versions()
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
	a1, b1 := versions()
	if a1-a0 != 7 || b1-b0 != 7 {
		t.Fatalf("versions active=%d staging=%d, want 7/7", a1-a0, b1-b0)
	}
	var attr, rel string
	if err := store.db.QueryRow(ctx, `SELECT attribute FROM fact WHERE novel_id=$1`, novelID).Scan(&attr); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(ctx, `SELECT rel_type FROM edge WHERE novel_id=$1`, novelID).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	if attr != "description" || rel != "ally" {
		t.Fatalf("sentinels changed: %q %q", attr, rel)
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
