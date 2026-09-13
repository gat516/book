package main

import "testing"

func TestKnownHostedProviderDiscardsBaseURLOverride(t *testing.T) {
	api := &API{}
	got, err := api.buildProviderConfigInput(providerConfigReq{
		Provider: "anthropic", Model: "claude", BaseURL: "https://proxy.example/v1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.BaseURL != "" {
		t.Fatalf("base URL = %q, want fixed provider endpoint", got.BaseURL)
	}
}

func TestCustomProviderRequiresSafeAbsoluteBaseURL(t *testing.T) {
	api := &API{}
	for _, raw := range []string{"", "models.example/v1", "https://user:pass@models.example/v1", "https://models.example/v1?q=secret"} {
		if _, err := api.buildProviderConfigInput(providerConfigReq{Provider: "custom", BaseURL: raw}); err == nil {
			t.Fatalf("base URL %q unexpectedly accepted", raw)
		}
	}
	got, err := api.buildProviderConfigInput(providerConfigReq{
		Provider: "custom", Model: "model-a", BaseURL: "https://models.example/v1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.BaseURL != "https://models.example/v1" {
		t.Fatalf("base URL = %q", got.BaseURL)
	}
}
