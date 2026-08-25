// Command scraper walks a novel's "next chapter" links and feeds ingest-api, so a novel
// can be ingested from a URL instead of pasted chapter by chapter (PLAN.md Phase N5).
//
// Deliberately per-site, not generic (docs/PLAN.md's own Milestone 2 tradeoff: "one line
// per site beats fighting a generic algorithm's edge cases"). Two sites are wired today
// (freewebnovel.go, shuhaige.go); adding a third is adding one file, not touching the
// walk loop.
package main

import "context"

// Page is one fetched chapter page. Title is the site's own printed chapter label
// (verbatim, e.g. "第4335章 北落邙山！(1/2)") — stored as inert metadata
// (source_meta.site_chapter_no) and NEVER parsed for numbering; the internal
// chapter_index is assigned sequentially as pages are walked, independent of whatever a
// site's own chapter/part scheme looks like (instructions.md §3.1).
type Page struct {
	Title   string
	Text    string
	NextURL string // absolute URL of the next chapter; "" means "no next link found"
}

// Site is the per-site adapter contract. Simpler than spec §3.2's generic
// Fetch(idx)/Next(prev) SourceAdapter shape: both sites here are pure next-link walkers
// (the whole page is fetched once and both content and the next link come out of that
// one request), so a single FetchPage call is the natural unit — there is no second
// strategy (e.g. catalog-indexed fetch) implemented yet to justify the extra indirection.
type Site interface {
	// Mode says whether fetched text has a separate original or not — "bootstrap" for an
	// already-translated site (raw_text and translated_text both get the same string, so
	// TranslateStage's early-out skips the LLM call), "translate" for a source-language
	// site the pipeline should machine-translate normally.
	Mode() string

	// FetchPage fetches and extracts one chapter page. notFound=true means "this chapter
	// doesn't exist" (e.g. a real HTTP 404) — an expected, clean stop condition, not an
	// error to log and retry.
	FetchPage(ctx context.Context, client *httpClient, pageURL string) (page Page, notFound bool, err error)
}

// siteFor picks the adapter whose host matches pageURL, or nil if none does.
func siteFor(host string) Site {
	switch host {
	case "freewebnovel.com", "www.freewebnovel.com":
		return freewebnovelSite{}
	case "m.shuhaige.net":
		return shuhaigeSite{}
	default:
		return nil
	}
}
