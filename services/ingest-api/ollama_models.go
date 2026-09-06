package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

func validateOllamaBaseURL(raw string, allowed map[string]bool) error {
	parsed, err := url.Parse(raw)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" ||
		(parsed.Path != "" && parsed.Path != "/") || parsed.RawQuery != "" || parsed.User != nil ||
		!allowed[strings.ToLower(parsed.Hostname())] {
		return fmt.Errorf("Ollama URL must use an allowed http(s) host with no path")
	}
	return nil
}

// listOllamaModels queries only Ollama's documented tags endpoint. A novel configured
// for Ollama uses its saved, allowlisted endpoint. Graph repair always runs against the
// server-local Ollama (KnowledgeEngine's loopback requirement), even when translation
// uses Gemini or another hosted provider, so its model picker falls back to that local
// endpoint instead of falsely treating a non-Ollama novel as an unreachable server.
func (a *API) listOllamaModels(w http.ResponseWriter, r *http.Request) {
	view, err := a.store.GetProviderConfig(r.Context(), r.PathValue("id"))
	if err != nil && !errors.Is(err, ErrProviderConfigNotFound) {
		writeErr(w, http.StatusNotFound, "no provider config for this novel")
		return
	}
	base := ""
	if err == nil && view.Provider == "ollama" {
		base = view.BaseURL
	}
	if base == "" {
		base = a.cfg.OllamaHost
	}
	if err := validateOllamaBaseURL(base, a.cfg.OllamaAllowedHosts); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	client := http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse // never let an allowed Ollama host redirect elsewhere
	}}
	resp, err := client.Get(strings.TrimRight(base, "/") + "/api/tags")
	if err != nil {
		writeErr(w, http.StatusBadGateway, "could not reach Ollama")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		writeErr(w, http.StatusBadGateway, fmt.Sprintf("Ollama returned %s", resp.Status))
		return
	}
	var payload struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		writeErr(w, http.StatusBadGateway, "invalid Ollama response")
		return
	}
	names := make([]string, 0, len(payload.Models))
	for _, model := range payload.Models {
		if model.Name != "" {
			names = append(names, model.Name)
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"models": names})
}
