package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

func TestFactEditsPreserveSourceAndAppendSemanticSuccessors(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	entityID := uuid.NewString()
	evidenceID := uuid.NewString()
	ontology := `{"kinds":["character"],"attributes":[{"name":"status","kinds":["character"]}]}`
	if _, err := store.db.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES($1,'Fact edit','zh','en',$2)`, novelID, ontology); err != nil {
		t.Fatal(err)
	}
	var legacy, revision string
	if err := store.db.QueryRow(ctx, `SELECT active_graph_revision::text FROM novel WHERE id=$1`, novelID).Scan(&legacy); err != nil {
		t.Fatal(err)
	}
	tx, err := store.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE graph_revision SET state='archived' WHERE id=$1`, legacy); err != nil {
		t.Fatal(err)
	}
	if err = tx.QueryRow(ctx, `INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology,model,snapshot) VALUES($1,'active',true,false,$2,'{"provider":"ollama","name":"test"}','{"chapters":[{"chapter":1,"source_hash":"hash"}]}') RETURNING id::text`, novelID, ontology).Scan(&revision); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE novel SET active_graph_revision=$2 WHERE id=$1`, novelID, revision); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `SELECT set_config('app.graph_revision',$1,true),set_config('app.graph_generation','1',true)`, revision); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO graph_evidence(id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote) VALUES($1,$2,$3,1,'hash',0,2,'活着')`, evidenceID, revision, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter,revision_id) VALUES($1,$2,'character','Hero',1,$3)`, entityID, novelID, revision); err != nil {
		t.Fatal(err)
	}
	var factID int64
	if err = tx.QueryRow(ctx, `INSERT INTO fact(novel_id,entity_id,attribute,value,valid_from_chapter,source_chapter,revision_id,evidence_id,claim_key) VALUES($1,$2,'status','活着',1,1,$3,$4,'seed') RETURNING id`, novelID, entityID, revision, evidenceID).Scan(&factID); err != nil {
		t.Fatal(err)
	}
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		store.db.Exec(context.Background(), `DELETE FROM fact_edit_audit WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM fact WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM entity WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM graph_evidence WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM novel WHERE id=$1`, novelID)
	})

	display, err := store.editFact(ctx, novelID, factID, "display", factEditRequest{RevisionID: revision, Version: 1, ValueEN: "alive", Actor: "reader"})
	if err != nil {
		t.Fatal(err)
	}
	if display.Version != 2 {
		t.Fatalf("display version=%d", display.Version)
	}
	var source, valueEN string
	if err = store.db.QueryRow(ctx, `SELECT value,value_en FROM fact WHERE id=$1`, factID).Scan(&source, &valueEN); err != nil {
		t.Fatal(err)
	}
	if source != "活着" || valueEN != "alive" {
		t.Fatalf("fact changed incorrectly: %q %q", source, valueEN)
	}
	corrected, err := store.editFact(ctx, novelID, factID, "correction", factEditRequest{RevisionID: revision, Version: 2, Attribute: "status", ValueEN: "missing", Actor: "reader"})
	if err != nil {
		t.Fatal(err)
	}
	if corrected.SuccessorID == nil || corrected.Version != 3 {
		t.Fatalf("correction=%+v", corrected)
	}
	var kind, successorSource string
	var supersedes int64
	if err = store.db.QueryRow(ctx, `SELECT kind,supersedes,value FROM fact WHERE id=$1`, *corrected.SuccessorID).Scan(&kind, &supersedes, &successorSource); err != nil {
		t.Fatal(err)
	}
	if kind != "correction" || supersedes != factID || successorSource != "活着" {
		t.Fatalf("successor lost provenance: %s %d %s", kind, supersedes, successorSource)
	}
	if _, err = store.editFact(ctx, novelID, *corrected.SuccessorID, "retraction", factEditRequest{RevisionID: revision, Version: 2, Actor: "reader"}); err != ErrFactStale {
		t.Fatalf("stale edit=%v", err)
	}
}

// A never-rebuilt novel's active revision is legacy=true, trusted=true (the
// initialize_graph_revision trigger's default). Before migration 0058 this returned
// ErrFactStale, the same 409 as "the version moved under you" — indistinguishable from a
// caller who just needed to reload and retry. There is nothing to reload here: the book
// has no managed graph to append a chapter's facts to until it is built.
func TestLegacyNovelReextractReturnsNoManagedGraph(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES($1,'Legacy novel','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		store.db.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		store.db.Exec(context.Background(), `DELETE FROM novel WHERE id=$1`, novelID)
	})

	var legacy bool
	if err := store.db.QueryRow(ctx, `SELECT r.legacy FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision WHERE n.id=$1`, novelID).Scan(&legacy); err != nil {
		t.Fatal(err)
	}
	if !legacy {
		t.Fatal("a fresh novel's active revision must be legacy")
	}

	if _, err := store.startChapterReextract(ctx, novelID, 1, "reader", "all"); err != ErrNoManagedGraph {
		t.Fatalf("legacy reextract error=%v, want ErrNoManagedGraph", err)
	}
}
