package main

import (
	"strings"
	"testing"
)

func TestValidateOllamaBaseURL(t *testing.T) {
	allowed := map[string]bool{
		"127.0.0.1":                    true,
		"cj-desktop.taila10bf4.ts.net": true,
	}
	tests := []struct {
		name    string
		raw     string
		wantErr string
	}{
		{name: "loopback with port", raw: "http://127.0.0.1:11434"},
		{name: "allowed tailscale host and root slash", raw: "http://cj-desktop.taila10bf4.ts.net/"},
		{name: "reject scheme", raw: "ftp://127.0.0.1:11434", wantErr: "scheme"},
		{name: "reject path", raw: "http://127.0.0.1:11434/api/tags", wantErr: "path"},
		{name: "reject unknown host", raw: "http://example.com:11434", wantErr: "OLLAMA_ALLOWED_HOSTS"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateOllamaBaseURL(tt.raw, allowed)
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("validateOllamaBaseURL(%q): %v", tt.raw, err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("validateOllamaBaseURL(%q) error = %v, want substring %q", tt.raw, err, tt.wantErr)
			}
		})
	}
}
