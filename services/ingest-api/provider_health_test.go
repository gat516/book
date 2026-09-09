package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func providerHealthTestServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Skipf("loopback listener unavailable: %v", err)
	}
	server := httptest.NewUnstartedServer(handler)
	server.Listener = listener
	server.Start()
	return server
}

func TestHostedProviderHealthUsesProviderModelEndpointsAndHeaders(t *testing.T) {
	tests := []struct {
		provider string
		path     string
		header   string
		want     string
	}{
		{provider: "gemini", path: "/v1beta/models", header: "x-goog-api-key", want: "ok"},
		{provider: "anthropic", path: "/v1/models", header: "x-api-key", want: "ok"},
		{provider: "deepseek", path: "/models", header: "Authorization", want: "ok"},
	}
	for _, tt := range tests {
		t.Run(tt.provider, func(t *testing.T) {
			server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != tt.path {
					t.Fatalf("path = %q, want %q", r.URL.Path, tt.path)
				}
				if got := r.Header.Get(tt.header); got == "" {
					t.Fatalf("missing %s header", tt.header)
				}
				if tt.provider == "deepseek" && r.Header.Get("Authorization") != "Bearer secret" {
					t.Fatalf("authorization header was not bearer encoded")
				}
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"models":[]}`))
			}))
			defer server.Close()

			if got := probeHostedProvider(context.Background(), tt.provider, server.URL, "secret"); got != tt.want {
				t.Fatalf("category = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestHostedProviderHealthMapsStatusesWithoutReturningBody(t *testing.T) {
	tests := []struct {
		name     string
		status   int
		body     string
		category string
	}{
		{name: "rejected", status: http.StatusUnauthorized, body: `{"error":"SECRET_REVOKED_KEY"}`, category: providerHealthCredentialRejected},
		{name: "forbidden", status: http.StatusForbidden, body: `account secret`, category: providerHealthCredentialRejected},
		{name: "missing model", status: http.StatusNotFound, body: `model details SECRET_MODEL`, category: providerHealthModelUnavailable},
		{name: "rate limited", status: http.StatusTooManyRequests, body: `try again later`, category: providerHealthRateLimited},
		{name: "per-minute quota", status: http.StatusTooManyRequests, body: `quota exceeded for requests per minute`, category: providerHealthRateLimited},
		{name: "quota", status: http.StatusTooManyRequests, body: `daily quota exhausted: SECRET`, category: providerHealthQuotaExhausted},
		{name: "server", status: http.StatusBadGateway, body: `provider traceback SECRET`, category: providerHealthModelServerError},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tt.status)
				_, _ = w.Write([]byte(tt.body))
			}))
			defer server.Close()

			got := probeHostedProvider(context.Background(), "deepseek", server.URL, "secret")
			if got != tt.category {
				t.Fatalf("category = %q, want %q", got, tt.category)
			}
			if strings.Contains(got, "SECRET") {
				t.Fatalf("provider body leaked through category %q", got)
			}
		})
	}
}

func TestLocalProviderHealthUsesTagsEndpoint(t *testing.T) {
	server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			t.Fatalf("path = %q, want /api/tags", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"models":[]}`))
	}))
	defer server.Close()
	if got := probeLocalProvider(context.Background(), server.URL); got != providerHealthOK {
		t.Fatalf("category = %q, want %q", got, providerHealthOK)
	}
}

func TestProviderHealthModelCatalogAcceptsPresentAndRejectsMissingModels(t *testing.T) {
	t.Run("ollama", func(t *testing.T) {
		server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"models":[{"name":"qwen3:8b"}]}`))
		}))
		defer server.Close()
		if got := probeLocalProvider(context.Background(), server.URL, "qwen3:8b"); got != providerHealthOK {
			t.Fatalf("present model category = %q", got)
		}
		if got := probeLocalProvider(context.Background(), server.URL, "missing"); got != providerHealthModelUnavailable {
			t.Fatalf("missing model category = %q", got)
		}
	})

	for _, test := range []struct {
		provider string
		body     string
		present  string
	}{
		{provider: "gemini", body: `{"models":[{"name":"models/gemini-2.5-flash"}]}`, present: "gemini-2.5-flash"},
		{provider: "anthropic", body: `{"data":[{"id":"claude-haiku-4-5"}]}`, present: "claude-haiku-4-5"},
		{provider: "deepseek", body: `{"data":[{"id":"deepseek-v4-flash"}]}`, present: "deepseek-v4-flash"},
	} {
		t.Run(test.provider, func(t *testing.T) {
			server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			if got := probeHostedProvider(context.Background(), test.provider, server.URL, "secret", test.present); got != providerHealthOK {
				t.Fatalf("present model category = %q", got)
			}
			if got := probeHostedProvider(context.Background(), test.provider, server.URL, "secret", "missing-model"); got != providerHealthModelUnavailable {
				t.Fatalf("missing model category = %q", got)
			}
		})
	}
}

func TestHostedProviderHealthReadsCatalogBeyondSixteenKiB(t *testing.T) {
	padding := strings.Repeat(`{"name":"models/padding-model"},`, 700)
	server := providerHealthTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"models":[` + padding + `{"name":"models/gemini-3.7-flash"}]}`))
	}))
	defer server.Close()

	if got := probeHostedProvider(context.Background(), "gemini", server.URL, "secret", "gemini-3.7-flash"); got != providerHealthOK {
		t.Fatalf("large catalog category = %q, want %q", got, providerHealthOK)
	}
}

func TestValidateHostedProviderBaseURLRequiresAllowlistedSafeHost(t *testing.T) {
	allowed := map[string]bool{"api.deepseek.com": true, "custom.example": true}
	for _, test := range []struct {
		name string
		raw  string
		want bool
	}{
		{name: "official", raw: "https://api.deepseek.com", want: true},
		{name: "custom allowlisted", raw: "https://custom.example/v1", want: true},
		{name: "unlisted", raw: "https://internal.example/v1", want: false},
		{name: "userinfo", raw: "https://user:pass@custom.example/v1", want: false},
		{name: "query", raw: "https://custom.example/v1?key=secret", want: false},
		{name: "wrong scheme", raw: "ftp://custom.example/v1", want: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			got := validateHostedProviderBaseURL("deepseek", test.raw, allowed) == nil
			if got != test.want {
				t.Fatalf("valid = %v, want %v", got, test.want)
			}
		})
	}
}
