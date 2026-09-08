package main

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/google/uuid"
)

// TestEveryValidRepairActionIsAcceptedByTheDatabase guards the exact gap that let
// "retry" reach production as a 500: validRepairAction() (Go) and repair_request's
// CHECK constraint (SQL) list the same actions in two places, and nothing enforced
// them staying in sync. Every action Go calls valid must also insert cleanly.
func TestEveryValidRepairActionIsAcceptedByTheDatabase(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := store.db.Exec(ctx, `INSERT INTO novel
		(id,title,source_lang,target_lang,ontology) VALUES($1,'Repair action check','zh','en','{}')`,
		novelID); err != nil {
		t.Fatal(err)
	}
	var revisionID string
	if err := store.db.QueryRow(ctx, `INSERT INTO graph_revision
		(novel_id,ontology) VALUES($1,'{}') RETURNING id::text`, novelID).Scan(&revisionID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = store.db.Exec(context.Background(), "DELETE FROM novel WHERE id=$1", novelID) })
	// 'extend' additionally requires an active, trusted, non-legacy graph revision
	// (RequestRepair's own precondition, not the CHECK constraint this test targets) --
	// the insert trigger's first revision is legacy, so satisfy that here.
	if _, err := store.db.Exec(ctx, `UPDATE graph_revision SET state='active',trusted=true,legacy=false
		WHERE id=(SELECT active_graph_revision FROM novel WHERE id=$1)`, novelID); err != nil {
		t.Fatal(err)
	}

	// chapter-scoped actions and 'prepare'/'extend' each carry their own required shape
	// (RequestRepair enforces that above the database); this test only needs the
	// constraint to accept one syntactically valid row per action, so give every action
	// a revision except the ones RequestRepair itself forbids from naming one.
	withoutRevision := map[string]bool{"prepare": true, "extend": true, "reextract": true, "reextract_apply": true}
	for _, action := range []string{"prepare", "review", "activate", "rollback",
		"reextract", "reextract_apply", "discard", "extend", "retry"} {
		body := repairRequestBody{Track: "graph", Action: action, RequestedBy: "test",
			Params: json.RawMessage(`{}`)}
		if !withoutRevision[action] {
			body.RevisionID = revisionID
		}
		if action == "extend" {
			body.Params = json.RawMessage(`{"upto_chapter":1}`)
		}
		if action == "reextract" || action == "reextract_apply" {
			chapter := 1
			body.ChapterIndex = &chapter
		}
		view, err := store.RequestRepair(ctx, novelID, body)
		if err != nil {
			t.Fatalf("action %q: RequestRepair rejected a row validRepairAction called valid: %v", action, err)
		}
		if _, err := store.db.Exec(ctx, "DELETE FROM repair_request WHERE id=$1", view.ID); err != nil {
			t.Fatal(err)
		}
	}
}
