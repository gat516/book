package main

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/PuerkitoBio/goquery"
)

// shuhaigeSite: fetched & inspected chapters of https://m.shuhaige.net/35212/ (混沌天帝訣
// / "Chaos Heavenly Emperor Technique" by 劍輕陽/Jian Qingyang — confirmed the same novel
// as freewebnovel's by protagonist name match, 凌峰/Ling Feng) this session.
//
// This replaces an earlier look.twword.com adapter: that site's robots.txt disallows all
// bots except a named allowlist of major crawlers (a blanket policy, not a path-specific
// rule), so this scraper — which honors robots.txt — could never fetch it. shuhaige.net's
// robots.txt has an explicit `User-agent: * / Allow: /`, so it's used instead as the raw
// (untranslated) continuation source.
//
// Content lives in <h1 class="headline"> (title) + <div class="content"> (paragraphs).
// Chapters are also split across multiple *pages* here (a "下一页"/"next page" link, not
// necessarily "下一章"/"next chapter") — the walk loop doesn't need to tell those apart:
// internal chapter_index increments per page visited regardless of what the site calls
// it, the same non-issue-by-design as twword's "(1/2)" parts would have been.
//
// mode is "translate": this is genuine source-language text the pipeline should MT.
type shuhaigeSite struct{}

// shuhaigeBoilerplate are substrings marking the site's own injected chrome rather than
// story text: a bookmark/promo line on every page, and a "continue to the next page" nag.
// Stripping them matters for two separate reasons, and the second one is not obvious:
//
//  1. It keeps site advertising out of the text handed to the translator, which would
//     otherwise be translated into the output verbatim and cost tokens to boot.
//  2. The nag is RANDOMIZED between several wordings per request, so the same page fetched
//     twice yields different bytes — which silently defeats content-hash deduplication.
//     Verified on real data this session: of 27 re-scraped chapters only 13 hashed
//     identically before stripping, and all 27 after.
//
// Matched as substrings, not exact lines, so a new nag variant sharing the same stem is
// still caught.
var shuhaigeBoilerplate = []string{
	"请点击下一页继续阅读", // every "this chapter continues, click next page" variant
	"请大家收藏",      // "…please bookmark (m.shuhaige.net)…" promo line
	"本章完",        // "(end of chapter)" marker the site appends to a chapter's last page
}

func isShuhaigeBoilerplate(text string) bool {
	for _, marker := range shuhaigeBoilerplate {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

func (shuhaigeSite) Mode() string { return "translate" }

func (shuhaigeSite) FetchPage(ctx context.Context, client *httpClient, pageURL string) (Page, bool, error) {
	resp, err := client.Get(ctx, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Page{}, true, nil
	}
	if resp.StatusCode != http.StatusOK {
		return Page{}, false, fmt.Errorf("shuhaige: unexpected status %d for %s", resp.StatusCode, pageURL)
	}

	doc, err := goquery.NewDocumentFromReader(resp.Body)
	if err != nil {
		return Page{}, false, fmt.Errorf("shuhaige: parse %s: %w", pageURL, err)
	}

	content := doc.Find("div.content").First()
	if content.Length() == 0 {
		return Page{}, false, fmt.Errorf("shuhaige: div.content not found on %s", pageURL)
	}
	title := strings.TrimSpace(doc.Find("h1.headline").First().Text())

	var paragraphs []string
	content.Find("p").Each(func(_ int, p *goquery.Selection) {
		if text := strings.TrimSpace(p.Text()); text != "" && !isShuhaigeBoilerplate(text) {
			paragraphs = append(paragraphs, text)
		}
	})

	// The pager appears twice (top and bottom of the page) with identical links; find
	// whichever "next" link is present, by its Chinese label rather than a class/id (the
	// pager div's class is shared with unrelated nav elements elsewhere on the page). A
	// multi-page chapter's non-final pages say "下一页" ("next page"); its final page
	// says "下一章" ("next chapter") instead — verified by fetching a real 3-page
	// chapter this session. The walk loop doesn't need to know which one it got: either
	// way it's "the next thing to fetch," consistent with chapter_index counting pages
	// visited rather than the site's own chapter/page numbering.
	var nextURL string
	doc.Find("a").EachWithBreak(func(_ int, a *goquery.Selection) bool {
		text := strings.TrimSpace(a.Text())
		if text != "下一页" && text != "下一章" {
			return true
		}
		if href, exists := a.Attr("href"); exists {
			nextURL = resolveURL(pageURL, href)
		}
		return false
	})

	return Page{
		Title:   title,
		Text:    strings.Join(paragraphs, "\n\n"),
		NextURL: nextURL,
	}, false, nil
}
