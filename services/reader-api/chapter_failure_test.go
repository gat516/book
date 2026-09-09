package main

import "testing"

func TestChapterFailureCategoryUsesOnlySafeErrorCodes(t *testing.T) {
	tests := []struct {
		code string
		want string
	}{
		{"provider_http_401", "credential_rejected"},
		{"provider_http_403", "credential_rejected"},
		{"provider_http_404", "model_not_available"},
		{"provider_http_408", "timeout"},
		{"provider_http_429", "rate_limited"},
		{"provider_connection", "unreachable"},
		{"provider_timeout", "timeout"},
		{"provider_http_500", "model_server_error"},
		{"provider_http_503", "model_server_error"},
		{"invalid_stage_output", "unknown"},
		{"", ""},
	}
	for _, test := range tests {
		if got := chapterFailureCategory(test.code); got != test.want {
			t.Errorf("chapterFailureCategory(%q) = %q, want %q", test.code, got, test.want)
		}
	}
}
