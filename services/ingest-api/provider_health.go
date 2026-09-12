package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// Provider health is deliberately a small, safe vocabulary.  In particular, the
// response never contains the provider's error body: those bodies can contain a key,
// an account identifier, or source text (instructions.md §0 and docs/provider-health-
// 2026-09-09.md Phase 2).
const (
	providerHealthOK                 = "ok"
	providerHealthUnreachable        = "unreachable"
	providerHealthCredentialMissing  = "credential_missing"
	providerHealthCredentialRejected = "credential_rejected"
	providerHealthRateLimited        = "rate_limited"
	providerHealthQuotaExhausted     = "quota_exhausted"
	providerHealthModelUnavailable   = "model_not_available"
	providerHealthModelServerError   = "model_server_error"
	providerHealthUnknown            = "unknown"
)

type providerHealthResponse struct {
	Provider     string `json:"provider"`
	EndpointKind string `json:"endpoint_kind"`
	State        string `json:"state"`
	Category     string `json:"category"`
}

type providerHealthConfig struct {
	provider     string
	model        string
	endpointKind string
	baseURL      string
	apiKey       string
}

type providerHealthCacheEntry struct {
	response providerHealthResponse
	status   int
	expires  time.Time
}

var errProviderHealthNovelNotFound = errors.New("novel not found")

func providerHealthTrack(track string) bool {
	switch track {
	case "translate", "extract":
		return true
	default:
		return false
	}
}

// providerHealthConfigFor resolves the same provider source used by execution (§5.4):
// the effective novel/account/process provider configuration.
//
// The graph and events tracks are gone with the revision tables they read (migration
// 0089). Extraction is pinned per record generation now, and a generation that disagrees
// with the novel's configured model is not a health question -- it is a new generation.
func (s *Store) providerHealthConfigFor(ctx context.Context, novelID, track string, cfg Config) (providerHealthConfig, error) {
	if !providerHealthTrack(track) {
		return providerHealthConfig{}, fmt.Errorf("unsupported track %q", track)
	}

	var provider, bookProvider, bookBaseURL *string
	var bookModel, bookTranslateModel, bookExtractModel *string
	err := s.db.QueryRow(ctx, `
		SELECT c.provider, c.base_url, c.model, c.translate_model, c.extract_model
		FROM novel n LEFT JOIN novel_provider_config c ON c.novel_id=n.id
		WHERE n.id=$1`, novelID).Scan(&bookProvider, &bookBaseURL, &bookModel, &bookTranslateModel, &bookExtractModel)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return providerHealthConfig{}, errProviderHealthNovelNotFound
		}
		return providerHealthConfig{}, fmt.Errorf("resolve %s provider: %w", track, err)
	}
	provider = bookProvider

	chosen := ""
	if provider != nil {
		chosen = strings.ToLower(strings.TrimSpace(*provider))
	}
	if chosen == "" {
		chosen = strings.ToLower(strings.TrimSpace(getenv("LLM_PROVIDER", "ollama")))
	}
	model := processProviderModel(track)
	if track == "translate" && bookTranslateModel != nil && strings.TrimSpace(*bookTranslateModel) != "" {
		model = strings.TrimSpace(*bookTranslateModel)
	} else if track == "extract" && bookExtractModel != nil && strings.TrimSpace(*bookExtractModel) != "" {
		model = strings.TrimSpace(*bookExtractModel)
	} else if bookModel != nil && strings.TrimSpace(*bookModel) != "" {
		model = strings.TrimSpace(*bookModel)
	}
	baseURL := ""
	if bookBaseURL != nil {
		baseURL = strings.TrimSpace(*bookBaseURL)
	}
	// Ollama work follows book URL -> account URL -> process host, matching
	// resolve_provider_config.
	if chosen == "ollama" {
		if baseURL == "" {
			accountBase, _, accountErr := s.accountCredential(ctx, chosen, cfg.ProviderConfigKey)
			if accountErr != nil {
				return providerHealthConfig{provider: chosen, model: model, endpointKind: "local"}, accountErr
			}
			baseURL = accountBase
			if baseURL == "" {
				baseURL = cfg.OllamaHost
			}
		}
		return providerHealthConfig{provider: chosen, model: model, endpointKind: "local", baseURL: baseURL}, nil
	}

	accountBase, accountKey, accountErr := s.accountCredential(ctx, chosen, cfg.ProviderConfigKey)
	if accountErr != nil {
		return providerHealthConfig{provider: chosen, model: model, endpointKind: "hosted"}, accountErr
	}
	if baseURL == "" {
		baseURL = accountBase
	}
	if accountKey == "" {
		accountKey = processProviderKey(chosen)
	}
	if baseURL == "" {
		baseURL = processProviderBase(chosen)
	}
	return providerHealthConfig{provider: chosen, model: model, endpointKind: "hosted", baseURL: baseURL, apiKey: accountKey}, nil
}

func processProviderModel(track string) string {
	if track == "translate" {
		return getenv("LLM_MODEL_TRANSLATE", "qwen2.5:14b")
	}
	return getenv("LLM_MODEL_EXTRACT", "qwen2.5:14b")
}

func processProviderKey(provider string) string {
	switch provider {
	case "anthropic":
		return os.Getenv("ANTHROPIC_API_KEY")
	case "deepseek":
		return os.Getenv("DEEPSEEK_API_KEY")
	case "gemini":
		return os.Getenv("GEMINI_API_KEY")
	case "groq":
		return os.Getenv("GROQ_API_KEY")
	default:
		return ""
	}
}

func processProviderBase(provider string) string {
	switch provider {
	case "anthropic":
		return "https://api.anthropic.com"
	case "deepseek":
		return getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
	case "gemini":
		return getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
	case "groq":
		return getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
	default:
		return ""
	}
}

// accountCredential decrypts only inside ingest-api. A ciphertext that cannot be
// decrypted is an internal configuration problem and becomes the safe unknown class;
// it is never sent to a reader.
func (s *Store) accountCredential(ctx context.Context, provider string, key [32]byte) (string, string, error) {
	base, secret, err := s.GetProviderCredential(ctx, provider, key)
	if errors.Is(err, ErrProviderCredentialNotFound) {
		return "", "", nil
	}
	if err != nil {
		return "", "", fmt.Errorf("provider credential unavailable: %w", err)
	}
	return base, secret, nil
}

func (a *API) providerHealth(w http.ResponseWriter, r *http.Request) {
	track := r.URL.Query().Get("track")
	if !providerHealthTrack(track) {
		writeErr(w, http.StatusBadRequest, "track must be translate or extract")
		return
	}
	config, err := a.store.providerHealthConfigFor(r.Context(), r.PathValue("id"), track, a.cfg)
	if errors.Is(err, errProviderHealthNovelNotFound) {
		writeErr(w, http.StatusNotFound, "novel not found")
		return
	}
	if err != nil {
		// Do not let decryption/database diagnostics cross the API boundary.
		writeJSON(w, http.StatusServiceUnavailable, providerHealthResponse{
			Provider: config.provider, EndpointKind: config.endpointKind,
			State: "unavailable", Category: providerHealthUnknown,
		})
		return
	}
	cacheKey := r.PathValue("id") + "\x00" + track
	if config.endpointKind == "hosted" {
		a.providerHealthMu.Lock()
		if entry, ok := a.providerHealthCache[cacheKey]; ok && time.Now().Before(entry.expires) {
			a.providerHealthMu.Unlock()
			writeJSON(w, entry.status, entry.response)
			return
		}
		a.providerHealthMu.Unlock()
	}

	var category string
	if config.endpointKind == "local" {
		if validateOllamaBaseURL(config.baseURL, a.cfg.OllamaAllowedHosts) != nil {
			category = providerHealthUnknown
		} else {
			category = probeLocalProvider(r.Context(), config.baseURL, config.model)
		}
	} else {
		if config.apiKey == "" {
			// Credential presence is the cheapest, most useful answer and does not
			// require validating or touching a user-supplied endpoint.
			category = providerHealthCredentialMissing
		} else if validateHostedProviderBaseURL(config.provider, config.baseURL, a.cfg.ProviderHealthAllowedHosts) != nil {
			category = providerHealthUnknown
		} else {
			category = probeHostedProvider(r.Context(), config.provider, config.baseURL, config.apiKey, config.model)
		}
	}
	resp := providerHealthResponse{
		Provider: config.provider, EndpointKind: config.endpointKind,
		State: "unavailable", Category: category,
	}
	status := http.StatusBadGateway
	if category == providerHealthOK {
		resp.State = "serving"
		status = http.StatusOK
	}
	if config.endpointKind == "hosted" {
		a.providerHealthMu.Lock()
		if a.providerHealthCache == nil {
			a.providerHealthCache = make(map[string]providerHealthCacheEntry)
		}
		a.providerHealthCache[cacheKey] = providerHealthCacheEntry{
			response: resp, status: status, expires: time.Now().Add(15 * time.Second),
		}
		a.providerHealthMu.Unlock()
	}
	writeJSON(w, status, resp)
}

var providerHealthRetryDelay = regexp.MustCompile(`(?i)"?retryDelay"?\s*:\s*"?([0-9]+(?:\.[0-9]+)?)s`)

func providerHealthLongQuota(body string, retryAfter ...string) bool {
	if len(retryAfter) > 0 {
		if seconds, err := strconv.ParseFloat(strings.TrimSpace(retryAfter[0]), 64); err == nil && seconds > 3600 {
			return true
		}
	}
	lower := strings.ToLower(body)
	for _, marker := range []string{"per-day", "per day", "daily", "24 hours", "day quota"} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	if match := providerHealthRetryDelay.FindStringSubmatch(body); len(match) == 2 {
		seconds, err := strconv.ParseFloat(match[1], 64)
		return err == nil && seconds > 3600
	}
	return false
}

func providerHealthStatus(status int, body string, retryAfter ...string) string {
	switch status {
	case http.StatusUnauthorized, http.StatusForbidden:
		return providerHealthCredentialRejected
	case http.StatusNotFound:
		return providerHealthModelUnavailable
	case http.StatusTooManyRequests:
		if providerHealthLongQuota(body, retryAfter...) {
			return providerHealthQuotaExhausted
		}
		return providerHealthRateLimited
	default:
		if status >= 500 {
			return providerHealthModelServerError
		}
		return providerHealthUnknown
	}
}

func providerHealthClient() *http.Client {
	return &http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
}

func probeLocalProvider(ctx context.Context, base string, requested ...string) string {
	if base == "" {
		return providerHealthUnreachable
	}
	parsed, err := url.Parse(base)
	if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return providerHealthUnreachable
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(base, "/")+"/api/tags", nil)
	if err != nil {
		return providerHealthUnreachable
	}
	resp, err := providerHealthClient().Do(req)
	if err != nil {
		return providerHealthUnreachable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return providerHealthStatus(resp.StatusCode, "")
	}
	var payload struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return providerHealthUnknown
	}
	if len(requested) > 0 && requested[0] != "" {
		found := false
		for _, model := range payload.Models {
			if model.Name == requested[0] {
				found = true
				break
			}
		}
		if !found {
			return providerHealthModelUnavailable
		}
	}
	return providerHealthOK
}

func validateHostedProviderBaseURL(provider, raw string, allowed map[string]bool) error {
	if provider != "gemini" && provider != "anthropic" && provider != "deepseek" && provider != "groq" {
		return fmt.Errorf("unsupported hosted provider")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" && u.Scheme != "https" || u.Hostname() == "" ||
		u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("invalid provider endpoint")
	}
	if !allowed[strings.ToLower(u.Hostname())] {
		return fmt.Errorf("provider endpoint host is not allowlisted")
	}
	return nil
}

func hostedModelsURL(provider, base string) (string, bool) {
	if base == "" {
		base = processProviderBase(provider)
	}
	u, err := url.Parse(strings.TrimRight(base, "/"))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", false
	}
	// Providers expose different catalog roots. For Gemini, the OpenAI-compatibility
	// base (/v1beta/openai) is not the catalog base; use the canonical /v1beta/models
	// path on the same origin.
	switch provider {
	case "gemini":
		u.Path = "/v1beta/models"
	case "anthropic":
		u.Path = "/v1/models"
	case "deepseek":
		u.Path = "/models"
	case "groq":
		u.Path = "/openai/v1/models"
	default:
		return "", false
	}
	u.RawQuery, u.Fragment, u.User = "", "", nil
	return u.String(), true
}

func probeHostedProvider(ctx context.Context, provider, base, apiKey string, requested ...string) string {
	if apiKey == "" {
		return providerHealthCredentialMissing
	}
	endpoint, ok := hostedModelsURL(provider, base)
	if !ok {
		return providerHealthUnknown
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return providerHealthUnknown
	}
	switch provider {
	case "gemini":
		req.Header.Set("x-goog-api-key", apiKey)
	case "anthropic":
		req.Header.Set("x-api-key", apiKey)
		req.Header.Set("anthropic-version", "2023-06-01")
	case "deepseek", "groq":
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}
	resp, err := providerHealthClient().Do(req)
	if err != nil {
		return providerHealthUnreachable
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		// Provider catalogs are routinely larger than error envelopes (Gemini's exceeds
		// 16 KiB). Keep this bounded, but large enough to decode the complete catalog;
		// unlike the diagnostic path below, these model identifiers contain no prose or
		// credentials and still never cross the health response boundary.
		body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		if err != nil {
			return providerHealthUnknown
		}
		if len(requested) > 0 && requested[0] != "" {
			found, valid := hostedCatalogContains(provider, body, requested[0])
			if !valid {
				return providerHealthUnknown
			}
			if !found {
				return providerHealthModelUnavailable
			}
		}
		return providerHealthOK
	}
	// Read only a bounded diagnostic for classifying quota vs ordinary rate limiting;
	// it is never included in the response.
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 16<<10))
	return providerHealthStatus(resp.StatusCode, string(body), resp.Header.Get("Retry-After"))
}

func hostedCatalogContains(provider string, body []byte, requested string) (found, valid bool) {
	switch provider {
	case "gemini":
		var payload struct {
			Models []struct {
				Name string `json:"name"`
			} `json:"models"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			return false, false
		}
		for _, model := range payload.Models {
			if strings.TrimPrefix(model.Name, "models/") == strings.TrimPrefix(requested, "models/") {
				return true, true
			}
		}
		return false, true
	case "anthropic", "deepseek", "groq":
		var payload struct {
			Data []struct {
				ID string `json:"id"`
			} `json:"data"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			return false, false
		}
		for _, model := range payload.Data {
			if model.ID == requested {
				return true, true
			}
		}
		return false, true
	default:
		return false, false
	}
}
