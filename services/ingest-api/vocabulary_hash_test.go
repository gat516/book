package main

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

func TestCanonicalVocabularyActions(t *testing.T) {
	for input, want := range map[string]string{
		"admit": "admit", "ban": "ban", "rename-to-alias": "alias",
		"set-cardinality": "cardinality", "set-kinds": "kinds", "edit-gloss": "gloss",
	} {
		if got := canonicalVocabularyAction(input); got != want {
			t.Errorf("%s => %s, want %s", input, got, want)
		}
	}
}

func TestVocabularyHashPayloadMatchesPipelineContract(t *testing.T) {
	req := vocabularyMutationRequest{TermType: "attribute", Name: "appearance", Action: "admit", Chapter: 12}
	payload, err := stableVocabularyPayload("9305a18f-1617-4e41-a6d2-3877df98eb7e", 7, req, nil, nil, "abc")
	if err != nil {
		t.Fatal(err)
	}
	wantPayload := `["9305a18f-1617-4e41-a6d2-3877df98eb7e",7,"attribute","appearance","admit",12,"abc"]`
	if string(payload) != wantPayload {
		t.Fatalf("payload=%s, want %s", payload, wantPayload)
	}
	sum := sha256.Sum256(append([]byte("abc"), payload...))
	if got := hex.EncodeToString(sum[:]); got != "5b2862d780b01c797519f1c8ad17a779217825aef4ed799547fc70e00cd11eb3" {
		t.Fatalf("hash=%s", got)
	}
}
