package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"
)

// walk follows next-links starting at startURL until a stop condition fires, calling
// onChapter for each complete source chapter in order. Adapters mark continuation pages,
// which are assembled before this callback. shouldStop is polled between website pages,
// but an incomplete source chapter is never ingested (instructions.md §3.1).
//
// Stop conditions (§7.2 M2.3, "don't trust a single signal"): a real not-found response,
// no next link on the page, or short content whose hash matches a page already seen
// this walk (a site serving the same placeholder page instead of a real 404). The hash is
// computed locally (crypto/sha256) rather than via textproc's HashContent RPC — this
// comparison is entirely within one scraper run's own fetched pages, never against a
// Python/Rust-computed hash, so there is no cross-language consistency requirement to
// justify standing up a gRPC client (and the protoc toolchain it needs) for it.
func walk(
	ctx context.Context,
	client *httpClient,
	site Site,
	startURL string,
	onChapter func(Page) error,
	shouldStop func(context.Context) (bool, error),
	waitForCapacity func(context.Context) error,
	contentLenFloor int,
) (stopReason string, err error) {
	seenHashes := make(map[string]bool)
	pageURL := startURL
	var chapter *Page

	for {
		stop, err := shouldStop(ctx)
		if err != nil {
			return "", err
		}
		if stop {
			return "cancelled", nil
		}

		// Block until the pipeline has room before spending a request. Checked here —
		// before the fetch, not after — so a full queue costs the source site nothing:
		// pausing after fetching would still hammer it at full rate.
		if err := waitForCapacity(ctx); err != nil {
			return "", err
		}

		page, notFound, err := fetchWithRetry(ctx, client, site, pageURL)
		if notFound {
			return "not_found", nil
		}
		if err != nil {
			return "", fmt.Errorf("fetch %s: %w", pageURL, err)
		}
		// Adapters extract content and next links; walk owns the exact URL that produced
		// this page, so provenance is attached here before ingestion.
		page.SourceURL = pageURL

		hash := contentHash(page.Text)
		if len(page.Text) < contentLenFloor && seenHashes[hash] {
			return "duplicate_content", nil
		}
		seenHashes[hash] = true

		if chapter == nil {
			assembled := page
			chapter = &assembled
		} else {
			chapter.Text += "\n\n" + page.Text
			chapter.NextURL = page.NextURL
			chapter.Continues = page.Continues
		}

		if page.Continues {
			if page.NextURL == "" {
				return "", fmt.Errorf("page %s says the chapter continues but has no next link", pageURL)
			}
			pageURL = page.NextURL
			continue
		}

		if err := onChapter(*chapter); err != nil {
			return "", fmt.Errorf("handle chapter at %s: %w", chapter.SourceURL, err)
		}
		chapter = nil

		if page.NextURL == "" {
			return "no_next_link", nil
		}
		pageURL = page.NextURL
	}
}

// fetchWithRetry backs off on transient failures (network errors, non-404 error status)
// rather than treating the first hiccup as a hard stop — a slow site is not the same
// signal as "chapter doesn't exist."
func fetchWithRetry(ctx context.Context, client *httpClient, site Site, pageURL string) (Page, bool, error) {
	const maxAttempts = 3
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(attempt) * 2 * time.Second
			select {
			case <-time.After(backoff):
			case <-ctx.Done():
				return Page{}, false, ctx.Err()
			}
		}
		page, notFound, err := site.FetchPage(ctx, client, pageURL)
		if notFound || err == nil {
			return page, notFound, err
		}
		lastErr = err
	}
	return Page{}, false, lastErr
}

func contentHash(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}
