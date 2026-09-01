package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

var ErrIngestUnavailable = errors.New("ingest-api unavailable")

// IngestClient proxies novel/chapter/glossary writes to ingest-api, the writer service.
// reader-api is the only thing the browser talks to (services/web/vite.config.ts's own
// comment says so); this mirrors AskClient's shape exactly (services/reader-api/ask.go)
// rather than inventing a second proxy pattern.
type IngestClient interface {
	QueueControl(ctx context.Context, method string, body json.RawMessage) (json.RawMessage, int, error)
	CreateNovel(ctx context.Context, body json.RawMessage) (json.RawMessage, int, error)
	DeleteNovel(ctx context.Context, novelID string) (json.RawMessage, int, error)
	PasteChapter(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	CorrectGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error)
	DeleteGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error)
	GetProviderConfig(ctx context.Context, novelID string) (json.RawMessage, int, error)
	ListProviderCredentials(ctx context.Context) (json.RawMessage, int, error)
	PutProviderCredential(ctx context.Context, provider string, body json.RawMessage) (json.RawMessage, int, error)
	DeleteProviderCredential(ctx context.Context, provider string) (json.RawMessage, int, error)
	PutProviderConfig(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	BootstrapGlossary(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	ApproveCharacterName(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error)
	TranslateAhead(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	UpdateNovelSettings(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
}

func (c *ingestHTTPClient) QueueControl(ctx context.Context, method string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, method, "/queue", body, true)
}

type ingestHTTPClient struct {
	baseURL string
	token   string
	http    *http.Client
}

func newIngestClient(cfg Config) IngestClient {
	return &ingestHTTPClient{
		baseURL: strings.TrimRight(cfg.IngestAPIURL, "/"),
		token:   cfg.IngestInternalToken,
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

// send forwards body to ingest-api at path via method, optionally with the internal
// bearer token (only POST /novels requires it — ingest-api is otherwise unauthenticated
// by design).
func (c *ingestHTTPClient) send(ctx context.Context, method, path string, body json.RawMessage, withToken bool) (json.RawMessage, int, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, 0, fmt.Errorf("build ingest-api request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if withToken {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("%w: %v", ErrIngestUnavailable, err)
	}
	defer resp.Body.Close()
	var result json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		// An empty body is not a malformed response. 204 No Content has none by
		// definition, and DELETE /provider-credentials returns exactly that -- decoding
		// it as an error made a successful delete report 502 to the browser.
		if !errors.Is(err, io.EOF) {
			return nil, 0, fmt.Errorf("invalid ingest-api response: %w", err)
		}
		result = nil
	}
	return result, resp.StatusCode, nil
}

func (c *ingestHTTPClient) CreateNovel(ctx context.Context, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels", body, true)
}

// DeleteNovel is token-gated on ingest-api's side, like CreateNovel — the two
// novel-lifecycle routes, one of them irreversible.
func (c *ingestHTTPClient) DeleteNovel(ctx context.Context, novelID string) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodDelete, "/novels/"+novelID, nil, true)
}

func (c *ingestHTTPClient) PasteChapter(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/chapters", body, false)
}

func (c *ingestHTTPClient) CorrectGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error) {
	path := "/novels/" + novelID + "/glossary/" + url.PathEscape(sourceTerm)
	return c.send(ctx, http.MethodPatch, path, body, false)
}

func (c *ingestHTTPClient) DeleteGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodDelete, "/novels/"+novelID+"/glossary/"+url.PathEscape(sourceTerm), body, false)
}

func (c *ingestHTTPClient) GetProviderConfig(ctx context.Context, novelID string) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodGet, "/novels/"+novelID+"/provider-config", nil, false)
}

func (c *ingestHTTPClient) PutProviderConfig(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPatch, "/novels/"+novelID+"/provider-config", body, false)
}

// Global provider credentials (migration 0035). Ungated like the other administration
// routes: they expose no chapter content, and reads are masked (api_key_set, never a key).
func (c *ingestHTTPClient) ListProviderCredentials(ctx context.Context) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodGet, "/provider-credentials", nil, false)
}

func (c *ingestHTTPClient) PutProviderCredential(ctx context.Context, provider string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPut, "/provider-credentials/"+provider, body, false)
}

func (c *ingestHTTPClient) DeleteProviderCredential(ctx context.Context, provider string) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodDelete, "/provider-credentials/"+provider, nil, false)
}

func (c *ingestHTTPClient) BootstrapGlossary(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/glossary/bootstrap", body, false)
}

func (c *ingestHTTPClient) ApproveCharacterName(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/name-reviews/"+url.PathEscape(sourceTerm)+"/approve", body, false)
}

func (c *ingestHTTPClient) TranslateAhead(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/translate-ahead", body, false)
}

func (c *ingestHTTPClient) UpdateNovelSettings(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPatch, "/novels/"+novelID+"/settings", body, false)
}
