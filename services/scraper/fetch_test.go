package main

import (
	"context"
	"fmt"
	"testing"
)

type fixtureSite struct {
	pages map[string]Page
}

func (fixtureSite) Mode() string { return "translate" }

func (s fixtureSite) FetchPage(
	_ context.Context, _ *httpClient, pageURL string,
) (Page, bool, error) {
	page, ok := s.pages[pageURL]
	if !ok {
		return Page{}, false, fmt.Errorf("missing fixture %s", pageURL)
	}
	return page, false, nil
}

func neverStop(context.Context) (bool, error) { return false, nil }
func noBackpressure(context.Context) error    { return nil }

func TestSiteForUsesSourceChapterAssemblyForFreshShuhaigeImports(t *testing.T) {
	site, ok := siteFor("m.shuhaige.net").(shuhaigeSite)
	if !ok || !site.assembleContinuations {
		t.Fatalf("unexpected Shuhaige adapter: %#v", site)
	}
}

func TestWalkAssemblesContinuationPagesIntoOneSourceChapter(t *testing.T) {
	site := fixtureSite{pages: map[string]Page{
		"https://example.test/chapter-1": {
			Title: "Chapter 1", Text: "first page", NextURL: "https://example.test/chapter-1-2", Continues: true,
		},
		"https://example.test/chapter-1-2": {
			Title: "Chapter 1", Text: "second page", NextURL: "https://example.test/chapter-2",
		},
		"https://example.test/chapter-2": {
			Title: "Chapter 2", Text: "next chapter",
		},
	}}
	var chapters []Page
	reason, err := walk(context.Background(), nil, site, "https://example.test/chapter-1",
		func(page Page) error {
			chapters = append(chapters, page)
			return nil
		}, neverStop, noBackpressure, 100)
	if err != nil {
		t.Fatal(err)
	}
	if reason != "no_next_link" || len(chapters) != 2 {
		t.Fatalf("reason=%q chapters=%d", reason, len(chapters))
	}
	first := chapters[0]
	if first.Text != "first page\n\nsecond page" {
		t.Fatalf("assembled text = %q", first.Text)
	}
	if first.SourceURL != "https://example.test/chapter-1" {
		t.Fatalf("source URL = %q", first.SourceURL)
	}
	if first.NextURL != "https://example.test/chapter-2" || first.Continues {
		t.Fatalf("assembled navigation = %#v", first)
	}
}

func TestWalkRejectsAnIncompleteSourceChapter(t *testing.T) {
	site := fixtureSite{pages: map[string]Page{
		"https://example.test/chapter-1": {
			Title: "Chapter 1", Text: "only page", Continues: true,
		},
	}}
	called := false
	_, err := walk(context.Background(), nil, site, "https://example.test/chapter-1",
		func(Page) error {
			called = true
			return nil
		}, neverStop, noBackpressure, 100)
	if err == nil || called {
		t.Fatalf("err=%v onChapter called=%v", err, called)
	}
}
