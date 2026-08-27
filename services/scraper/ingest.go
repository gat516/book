package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// ingestClient posts scraped chapters to ingest-api's existing paste endpoint. The
// scraper is a second producer of paste-shaped requests (CLAUDE.md: "ingest-api is
// deliberately thin") — it never writes to Postgres/MinIO directly.
type ingestClient struct {
	baseURL string
	http    *http.Client
}

func newIngestClient(baseURL string) *ingestClient {
	return &ingestClient{baseURL: strings.TrimRight(baseURL, "/"), http: &http.Client{Timeout: 30 * time.Second}}
}

type pasteChapterRequest struct {
	ChapterIndex   int    `json:"chapter_index"`
	RawText        string `json:"raw_text"`
	TranslatedText string `json:"translated_text,omitempty"`
	SiteChapterNo  string `json:"site_chapter_no,omitempty"`
	Part           int    `json:"part,omitempty"`
}

type pasteChapterResponse struct {
	ChapterIndex int  `json:"chapter_index"`
	Duplicate    bool `json:"duplicate"`
}

// PasteChapter posts one scraped page. It reports whether ingest-api recognised the body
// as already-ingested content (migration 0013's content-hash dedup): a re-run over pages
// this novel already holds answers 200 + duplicate:true and writes nothing, where a fresh
// page answers 202. Both are success — only a genuine failure returns an error.
func (c *ingestClient) PasteChapter(ctx context.Context, novelID string, req pasteChapterRequest) (bool, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return false, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		fmt.Sprintf("%s/novels/%s/chapters", c.baseURL, novelID), bytes.NewReader(body))
	if err != nil {
		return false, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(httpReq)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted && resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("ingest-api returned %d for chapter %d", resp.StatusCode, req.ChapterIndex)
	}
	var parsed pasteChapterResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return false, fmt.Errorf("decode ingest-api response for chapter %d: %w", req.ChapterIndex, err)
	}
	return parsed.Duplicate, nil
}
