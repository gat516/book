package main

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func TestRepairActionsIncludeGraphExtension(t *testing.T) {
	for _, action := range []string{"extend", "reextract_apply"} {
		if !validRepairAction(action) {
			t.Fatalf("%q should be a valid repair action", action)
		}
	}
}

func TestExtendValidationRunsBeforeDatabaseAccess(t *testing.T) {
	novelID := "00000000-0000-0000-0000-000000000001"
	cases := []repairRequestBody{
		{Track: "graph", Action: "extend", Params: json.RawMessage(`{}`), RequestedBy: "operator"},
		{Track: "events", Action: "extend", Params: json.RawMessage(`{"upto_chapter":2}`), RequestedBy: "operator"},
		{Track: "graph", Action: "extend", Params: json.RawMessage(`{"upto_chapter":1.5}`), RequestedBy: "operator"},
		{Track: "graph", Action: "extend", RevisionID: novelID, Params: json.RawMessage(`{"upto_chapter":2}`), RequestedBy: "operator"},
	}
	store := &Store{}
	for _, body := range cases {
		_, err := store.RequestRepair(context.Background(), novelID, body)
		if !errors.Is(err, ErrRepairInvalid) {
			t.Fatalf("body %+v: expected ErrRepairInvalid, got %v", body, err)
		}
	}
}
