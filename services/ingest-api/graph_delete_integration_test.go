package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

func TestDeleteGraphPreservesBookContentAndReaderState(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO novel
		(id,title,source_lang,target_lang,ontology)
		VALUES($1,'Graph deletion','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO chapter
		(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status)
		VALUES($1,1,'hash','raw/test.txt','{}','done')`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO glossary
		(novel_id,source_term,target_term,version,locked_at_chapter)
		VALUES($1,'凌峰','Ling Feng',1,1)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO reader_progress
		(reader_id,novel_id,current_chapter) VALUES('reader-a',$1,1)`, novelID); err != nil {
		t.Fatal(err)
	}
	var activeRevision string
	if err := store.db.QueryRow(ctx,
		"SELECT active_graph_revision::text FROM novel WHERE id=$1", novelID).Scan(&activeRevision); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO chapter_knowledge_run
		(novel_id,chapter_index,revision_id,mode,input_hash,display_hash,model_identity,graph_generation,graph_version)
		VALUES($1,1,$2,'ordinary','input','display','model',1,1)`, novelID, activeRevision); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(ctx, `INSERT INTO graph_revision
		(novel_id,ontology) VALUES($1,'{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = store.db.Exec(context.Background(), "DELETE FROM novel WHERE id=$1", novelID) })

	deleted, err := store.deleteGraph(ctx, novelID)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 2 { // the insert trigger's active legacy revision plus our staging one
		t.Fatalf("deleted revisions=%d, want 2", deleted)
	}

	var revisions int
	var active *string
	if err = store.db.QueryRow(ctx, `SELECT active_graph_revision::text,
		(SELECT count(*) FROM graph_revision WHERE novel_id=$1)
		FROM novel WHERE id=$1`, novelID).Scan(&active, &revisions); err != nil {
		t.Fatal(err)
	}
	if active != nil || revisions != 0 {
		t.Fatalf("active=%v revisions=%d", active, revisions)
	}
	var runs int
	if err = store.db.QueryRow(ctx,
		"SELECT count(*) FROM chapter_knowledge_run WHERE novel_id=$1", novelID).Scan(&runs); err != nil {
		t.Fatal(err)
	}
	if runs != 0 {
		t.Fatalf("chapter knowledge runs=%d, want 0", runs)
	}
	for name, query := range map[string]string{
		"chapter":  "SELECT count(*) FROM chapter WHERE novel_id=$1",
		"glossary": "SELECT count(*) FROM glossary WHERE novel_id=$1",
		"progress": "SELECT count(*) FROM reader_progress WHERE novel_id=$1",
	} {
		var count int
		if err = store.db.QueryRow(ctx, query, novelID).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("%s rows=%d, want 1", name, count)
		}
	}
}
