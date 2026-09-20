package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Preview reads one page exactly the way a scrape would and reports what it found,
// without ingesting anything. It is what makes an unknown site safe to try: the generic
// reader (generic.go) guesses where a site keeps its text, and a guess is worth looking
// at before a few hundred chapters are fetched on the strength of it.
type Preview struct {
	URL  string `json:"url"`
	Host string `json:"host"`
	// Reader is "built-in" when the host has its own adapter, "generic" otherwise.
	Reader string `json:"reader"`
	// RobotsAllowed is false when robots.txt disallows this path. The scrape would refuse
	// it, so nothing else here is filled in.
	RobotsAllowed bool `json:"robots_allowed"`
	// Excerpt is the opening of the extracted text, enough to tell prose from a menu.
	Excerpt    string `json:"excerpt,omitempty"`
	Title      string `json:"title,omitempty"`
	TextChars  int    `json:"text_chars"`
	Paragraphs int    `json:"paragraphs"`
	NextURL    string `json:"next_url,omitempty"`
	// Continues means the next link is another page of this same chapter, which the
	// walker assembles into one chapter rather than two (§3.1).
	Continues bool `json:"continues"`
	// SuggestedMode is "translate" when the page reads as source-language text and
	// "bootstrap" when it reads as an existing translation. The reader still chooses.
	SuggestedMode string `json:"suggested_mode,omitempty"`
	Error         string `json:"error,omitempty"`
}

const previewExcerptRunes = 320

func preview(ctx context.Context, client *httpClient, rawURL string, contentLenFloor int) Preview {
	out := Preview{URL: rawURL}
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" || !strings.HasPrefix(parsed.Scheme, "http") {
		out.Error = "That is not an http(s) URL."
		return out
	}
	out.Host = parsed.Host
	out.Reader = "generic"
	if builtInSite(parsed.Host) {
		out.Reader = "built-in"
	}

	allowed, err := client.robotsAllow(ctx, parsed.Scheme+"://"+parsed.Host, parsed.Path)
	if err != nil {
		allowed = true // no robots.txt to read is not a refusal; get() treats it the same
	}
	out.RobotsAllowed = allowed
	if !allowed && out.Reader != "built-in" {
		out.Error = "This site's robots.txt disallows crawling this page, so a scrape would stop here."
		return out
	}

	page, notFound, err := siteFor(parsed.Host, contentLenFloor).FetchPage(ctx, client, rawURL)
	switch {
	case notFound:
		out.Error = "That page does not exist (404)."
		return out
	case err != nil:
		out.Error = err.Error()
		return out
	}

	text := []rune(page.Text)
	out.Title, out.TextChars = page.Title, len(text)
	out.Paragraphs = strings.Count(page.Text, "\n\n") + 1
	out.NextURL, out.Continues = page.NextURL, page.Continues
	out.Excerpt = string(text[:min(len(text), previewExcerptRunes)])
	out.SuggestedMode = "bootstrap"
	if cjkShare(page.Text) > 0.2 {
		out.SuggestedMode = "translate"
	}
	return out
}

// serveHTTP runs the preview endpoint beside the worker loop. The scraper owns this
// because it owns the adapters and the politeness rules: previewing through a second
// implementation would answer for a fetch the scrape would not actually make.
func serveHTTP(ctx context.Context, addr string, client *httpClient, contentLenFloor int) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /preview", func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			URL string `json:"url"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10)).Decode(&request); err != nil {
			http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
			return
		}
		// One page fetch, rate-limited like any other: a preview must not become a way to
		// hit a site faster than a scrape would.
		fetchCtx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(preview(fetchCtx, client, strings.TrimSpace(request.URL), contentLenFloor))
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})

	server := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	go func() {
		log.Printf("scraper preview listening on %s", addr)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("preview server: %v", err)
		}
	}()
}
