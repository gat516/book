package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"novel-engine/platform/tenant"
	"os"
	"strings"
	"time"
)

// ScraperClient asks the scraper what a URL extracts to before a scrape is started. The
// scraper answers because it owns the adapters and the politeness rules; reader-api
// re-implementing either would preview a fetch the real scrape would not make.
type ScraperClient interface {
	Preview(ctx context.Context, url string) (json.RawMessage, error)
}

type scraperHTTPClient struct {
	url  string
	http *http.Client
}

func newScraperClient(cfg Config) ScraperClient {
	return &scraperHTTPClient{
		url: strings.TrimRight(cfg.ScraperURL, "/") + "/preview",
		// Generous: one polite fetch waits on the scraper's own rate limiter first.
		http: &http.Client{Timeout: 90 * time.Second},
	}
}

func (c *scraperHTTPClient) Preview(ctx context.Context, pageURL string) (json.RawMessage, error) {
	body, _ := json.Marshal(map[string]string{"url": pageURL})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build preview request: %w", err)
	}
	tenant.Forward(ctx, req, os.Getenv("INGEST_INTERNAL_TOKEN"))
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("scraper unavailable: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return nil, fmt.Errorf("read preview response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("scraper preview failed (%d)", resp.StatusCode)
	}
	return raw, nil
}

// postScrapePreview reads one page and reports what a scrape would extract from it.
// Ungated like the scrape it precedes: this is ingestion, not reading, and the page comes
// from the public web rather than from the book's gated text.
func (a *API) postScrapePreview(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if _, ok := pathUUID(r, "id"); !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	var request struct {
		URL string `json:"url"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if strings.TrimSpace(request.URL) == "" {
		writeError(w, http.StatusBadRequest, "start_url is required")
		return
	}
	if a.scraper == nil {
		writeError(w, http.StatusBadGateway, "scraper unavailable")
		return
	}
	result, err := a.scraper.Preview(r.Context(), request.URL)
	if err != nil {
		log.Printf("scrape preview: %v", err)
		writeError(w, http.StatusBadGateway, "scraper unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(result)
}
