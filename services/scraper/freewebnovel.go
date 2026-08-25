package main

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/PuerkitoBio/goquery"
)

// freewebnovelSite: fetched & inspected chapter-1 and a deliberately-invalid chapter-9999
// of https://freewebnovel.com/novel/chaos-heavenly-emperor-technique/ this session.
//
// Content lives in #article (an <h4> title then <p> paragraphs, with ad blocks
// (.reader-ad-skip) interleaved that must be stripped before extracting text). The next
// link is #next_url — a stable, unique-id selector, the simplest case this scraper
// handles. A chapter past the last translated one returns a genuine HTTP 404 (verified),
// so mode is "bootstrap": this site is an existing fan translation with no separate
// original text available to us at all.
type freewebnovelSite struct{}

func (freewebnovelSite) Mode() string { return "bootstrap" }

func (freewebnovelSite) FetchPage(ctx context.Context, client *httpClient, pageURL string) (Page, bool, error) {
	resp, err := client.Get(ctx, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Page{}, true, nil
	}
	if resp.StatusCode != http.StatusOK {
		return Page{}, false, fmt.Errorf("freewebnovel: unexpected status %d for %s", resp.StatusCode, pageURL)
	}

	doc, err := goquery.NewDocumentFromReader(resp.Body)
	if err != nil {
		return Page{}, false, fmt.Errorf("freewebnovel: parse %s: %w", pageURL, err)
	}

	article := doc.Find("#article").First()
	if article.Length() == 0 {
		return Page{}, false, fmt.Errorf("freewebnovel: #article not found on %s", pageURL)
	}
	title := strings.TrimSpace(article.Find("h4").First().Text())
	article.Find(".reader-ad-skip").Remove()

	var paragraphs []string
	article.Find("p").Each(func(_ int, p *goquery.Selection) {
		if text := strings.TrimSpace(p.Text()); text != "" {
			paragraphs = append(paragraphs, text)
		}
	})

	nextHref, exists := doc.Find("#next_url").First().Attr("href")
	var nextURL string
	if exists {
		nextURL = resolveURL(pageURL, nextHref)
	}

	return Page{
		Title:   title,
		Text:    strings.Join(paragraphs, "\n\n"),
		NextURL: nextURL,
	}, false, nil
}

// resolveURL joins a possibly-relative href against the page it was found on. Both sites
// here emit root-relative hrefs (e.g. "/novel/.../chapter-2"), but resolving properly
// costs nothing and doesn't assume that stays true.
func resolveURL(pageURL, href string) string {
	base, err := url.Parse(pageURL)
	if err != nil {
		return href
	}
	ref, err := url.Parse(href)
	if err != nil {
		return href
	}
	return base.ResolveReference(ref).String()
}
