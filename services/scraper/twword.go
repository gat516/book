package main

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/PuerkitoBio/goquery"
)

// twwordSite: fetched & inspected a chapter of https://look.twword.com/02051597/ this
// session — the raw (untranslated) Chinese source, continuing past where the English fan
// translation on freewebnovel currently stops.
//
// Content lives in .chapter-content .content (an <h1> title, then paragraphs, with ad
// blocks (.gadBlock) interleaved). The "Next" control is a JS-driven <span
// class="nextBtn">, not a plain <a href> — goquery alone can't find a next link there.
// The target URL sits in plain text in a <script> tag instead: `var nextUrl =
// '/02051597/8095_4338_2.html';` — extracted with a regex, no headless browser needed.
//
// Chapter titles here confirm the source's "parts" quirk (e.g. "第4335章 北落邙山！
// (1/2)", continuing at a "..._2.html" URL whose own chapter id does NOT increment for
// part 2). This is a non-issue by design: Title is stored verbatim as inert metadata
// (source_meta.site_chapter_no) and never parsed — the internal chapter_index the walk
// loop assigns is what actually orders chapters, completely independent of this.
//
// mode is "translate": this is genuine source-language text the pipeline should MT.
type twwordSite struct{}

func (twwordSite) Mode() string { return "translate" }

var twwordNextURLPattern = regexp.MustCompile(`nextUrl\s*=\s*'([^']*)'`)

func (twwordSite) FetchPage(ctx context.Context, client *httpClient, pageURL string) (Page, bool, error) {
	resp, err := client.Get(ctx, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Page{}, true, nil
	}
	if resp.StatusCode != http.StatusOK {
		return Page{}, false, fmt.Errorf("twword: unexpected status %d for %s", resp.StatusCode, pageURL)
	}

	doc, err := goquery.NewDocumentFromReader(resp.Body)
	if err != nil {
		return Page{}, false, fmt.Errorf("twword: parse %s: %w", pageURL, err)
	}

	content := doc.Find(".chapter-content .content").First()
	if content.Length() == 0 {
		return Page{}, false, fmt.Errorf("twword: .chapter-content .content not found on %s", pageURL)
	}
	title := strings.TrimSpace(doc.Find(".chapter-content h1").First().Text())
	content.Find(".gadBlock, script").Remove()

	var paragraphs []string
	content.Find("p").Each(func(_ int, p *goquery.Selection) {
		if text := strings.TrimSpace(p.Text()); text != "" {
			paragraphs = append(paragraphs, text)
		}
	})

	var nextURL string
	doc.Find("script").EachWithBreak(func(_ int, s *goquery.Selection) bool {
		match := twwordNextURLPattern.FindStringSubmatch(s.Text())
		if match == nil {
			return true // keep looking
		}
		if match[1] != "" {
			nextURL = resolveURL(pageURL, match[1])
		}
		return false // found the declaration (even if empty = no next); stop
	})

	return Page{
		Title:   title,
		Text:    strings.Join(paragraphs, "\n\n"),
		NextURL: nextURL,
	}, false, nil
}
