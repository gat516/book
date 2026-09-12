package main

import "testing"

func TestRecordReviewValidationAndFingerprint(t *testing.T) {
	req := recordReviewRequest{
		RowID: "22222222-2222-4222-8222-222222222222", Decision: "accepted",
		Reason: "verified", RequestID: "request-1", Actor: "reader-a",
	}
	if err := validateRecordReviewRequest("11111111-1111-4111-8111-111111111111", &req); err != nil {
		t.Fatalf("valid request rejected: %v", err)
	}
	if got := recordReviewFingerprint(req); got == "" || len(got) != 64 {
		t.Fatalf("fingerprint=%q, want sha256 hex", got)
	}
	changed := req
	changed.Reason = "different"
	if recordReviewFingerprint(req) == recordReviewFingerprint(changed) {
		t.Fatal("request reason did not affect idempotency fingerprint")
	}
	for _, decision := range []string{"pass", "reject", ""} {
		bad := req
		bad.Decision = decision
		if err := validateRecordReviewRequest("11111111-1111-4111-8111-111111111111", &bad); err == nil {
			t.Fatalf("decision %q accepted", decision)
		}
	}
}
