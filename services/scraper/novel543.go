package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/PuerkitoBio/goquery"
)

// novel543Site extracts one source chapter from novel543.com's reader pages. A source
// chapter may span URLs ending in _2, _3, ...; FetchPage marks those links as
// continuations and the shared walk loop assembles them before ingestion (§3.1).
type novel543Site struct{}

var novel543PartSuffix = regexp.MustCompile(`\s*\((\d+)\s*/\s*(\d+)\)\s*$`)

func (novel543Site) Mode() string { return "translate" }

func novel543TitleAndContinuation(title string) (string, bool) {
	title = strings.TrimSpace(title)
	match := novel543PartSuffix.FindStringSubmatch(title)
	if len(match) != 3 {
		return title, false
	}
	part, partErr := strconv.Atoi(match[1])
	total, totalErr := strconv.Atoi(match[2])
	if partErr != nil || totalErr != nil || part < 1 || total < 1 || part > total {
		return title, false
	}
	return strings.TrimSpace(novel543PartSuffix.ReplaceAllString(title, "")), part < total
}

func isNovel543Boilerplate(text string) bool {
	text = strings.TrimSpace(text)
	return text == "" || strings.HasPrefix(text, "溫馨提示:") ||
		strings.HasPrefix(text, "温馨提示:")
}

func (novel543Site) FetchPage(
	ctx context.Context, client *httpClient, pageURL string,
) (Page, bool, error) {
	// This adapter is the one explicit site-policy exception configured by the owner.
	// Rate limiting, jitter, timeouts, and the honest user agent still apply.
	resp, err := client.GetIgnoringRobots(ctx, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Page{}, true, nil
	}
	if resp.StatusCode != http.StatusOK {
		return Page{}, false, fmt.Errorf("novel543: unexpected status %d for %s", resp.StatusCode, pageURL)
	}

	page, err := parseNovel543Page(resp.Body, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	return page, false, nil
}

func parseNovel543Page(body io.Reader, pageURL string) (Page, error) {
	doc, err := goquery.NewDocumentFromReader(body)
	if err != nil {
		return Page{}, fmt.Errorf("novel543: parse %s: %w", pageURL, err)
	}
	chapter := doc.Find("#chapterWarp .chapter-content").First()
	if chapter.Length() == 0 {
		return Page{}, fmt.Errorf("novel543: chapter content not found on %s", pageURL)
	}
	title, continues := novel543TitleAndContinuation(chapter.Find("h1").First().Text())
	if title == "" {
		return Page{}, fmt.Errorf("novel543: chapter title not found on %s", pageURL)
	}

	var paragraphs []string
	chapter.Find(".content p").Each(func(_ int, p *goquery.Selection) {
		if text := strings.TrimSpace(p.Text()); !isNovel543Boilerplate(text) {
			paragraphs = append(paragraphs, text)
		}
	})
	if len(paragraphs) == 0 {
		return Page{}, fmt.Errorf("novel543: no chapter paragraphs found on %s", pageURL)
	}

	var nextURL string
	doc.Find(".foot-nav a").EachWithBreak(func(_ int, a *goquery.Selection) bool {
		if strings.TrimSpace(a.Text()) != "下一章" {
			return true
		}
		if href, exists := a.Attr("href"); exists {
			nextURL = resolveURL(pageURL, href)
		}
		return false
	})

	return Page{
		Title: title, Text: strings.Join(paragraphs, "\n\n"),
		NextURL: nextURL, Continues: continues,
	}, nil
}
