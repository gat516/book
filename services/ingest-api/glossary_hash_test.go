package main

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

// Golden values computed directly from Python (the source of truth,
// services/pipeline/pipeline/stages/resolve.py's _lock_glossary):
//
//	json.dumps([novel_id, seq, source_term, old_target, new_target, chapter, prev_hash],
//	           ensure_ascii=False, separators=(",", ":"))
//	hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()
//
// Deliberately exercises '<', '>', '&' (which Go's encoding/json HTML-escapes by
// default but Python's json.dumps never does) and non-ASCII CJK text, chained across two
// rows (case 2's prev_hash is case 1's row_hash) — this is the mandatory guard for the
// accepted risk of porting the hash-chain to Go (PLAN.md Phase N2).
func TestPythonJSONArrayMatchesPythonJSONDumps(t *testing.T) {
	tests := []struct {
		name        string
		novelID     string
		seq         int
		sourceTerm  string
		oldTarget   string
		newTarget   string
		chapter     int
		prevHash    string
		wantPayload string
		wantRowHash string
	}{
		{
			name:        "insert-shaped row, empty old_target and prev_hash",
			novelID:     "test-novel-id",
			seq:         1,
			sourceTerm:  "青云宗<test>",
			oldTarget:   "",
			newTarget:   "Azure & Cloud>Sect",
			chapter:     5,
			prevHash:    "",
			wantPayload: `["test-novel-id",1,"青云宗<test>","","Azure & Cloud>Sect",5,""]`,
			wantRowHash: "4ff2e0c038f9215288893e4b1c4ec44874488f5d598ff2ead98ee2b2c8a0edc0",
		},
		{
			name:        "correction-shaped row, chained prev_hash",
			novelID:     "test-novel-id",
			seq:         2,
			sourceTerm:  "青云宗<test>",
			oldTarget:   "Old & Term>Value",
			newTarget:   "New <Value>",
			chapter:     10,
			prevHash:    "4ff2e0c038f9215288893e4b1c4ec44874488f5d598ff2ead98ee2b2c8a0edc0",
			wantPayload: `["test-novel-id",2,"青云宗<test>","Old & Term>Value","New <Value>",10,"4ff2e0c038f9215288893e4b1c4ec44874488f5d598ff2ead98ee2b2c8a0edc0"]`,
			wantRowHash: "b633065fecf00c94a9b0d7099c820cd82fdcb77b7b5daa3ee1a1a16f7c0cb373",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			payload := pythonJSONArray(tt.novelID, tt.seq, tt.sourceTerm, tt.oldTarget, tt.newTarget, tt.chapter, tt.prevHash)
			if payload != tt.wantPayload {
				t.Fatalf("payload = %q, want %q", payload, tt.wantPayload)
			}
			sum := sha256.Sum256([]byte(tt.prevHash + payload))
			rowHash := hex.EncodeToString(sum[:])
			if rowHash != tt.wantRowHash {
				t.Fatalf("row_hash = %q, want %q", rowHash, tt.wantRowHash)
			}
		})
	}
}
