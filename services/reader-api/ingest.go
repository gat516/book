package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
	CreateNovel(ctx context.Context, body json.RawMessage) (json.RawMessage, int, error)
	PasteChapter(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	CorrectGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error)
	GetProviderConfig(ctx context.Context, novelID string) (json.RawMessage, int, error)
	PutProviderConfig(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
	BootstrapGlossary(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error)
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
		return nil, 0, fmt.Errorf("invalid ingest-api response: %w", err)
	}
	return result, resp.StatusCode, nil
}

func (c *ingestHTTPClient) CreateNovel(ctx context.Context, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels", body, true)
}

func (c *ingestHTTPClient) PasteChapter(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/chapters", body, false)
}

func (c *ingestHTTPClient) CorrectGlossaryTerm(ctx context.Context, novelID, sourceTerm string, body json.RawMessage) (json.RawMessage, int, error) {
	path := "/novels/" + novelID + "/glossary/" + url.PathEscape(sourceTerm)
	return c.send(ctx, http.MethodPatch, path, body, false)
}

func (c *ingestHTTPClient) GetProviderConfig(ctx context.Context, novelID string) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodGet, "/novels/"+novelID+"/provider-config", nil, false)
}

func (c *ingestHTTPClient) PutProviderConfig(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPatch, "/novels/"+novelID+"/provider-config", body, false)
}

func (c *ingestHTTPClient) BootstrapGlossary(ctx context.Context, novelID string, body json.RawMessage) (json.RawMessage, int, error) {
	return c.send(ctx, http.MethodPost, "/novels/"+novelID+"/glossary/bootstrap", body, false)
}
