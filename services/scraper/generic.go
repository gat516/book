package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode"

	"github.com/PuerkitoBio/goquery"
	"golang.org/x/net/html"
)

// genericSite reads a chapter page without knowing the site. It is the fallback for any
// host without its own adapter, so a novel can be ingested from a URL nobody has written
// code for. A hand-written adapter still wins for the hosts that have one (adapter.go):
// this guesses, and a guess can be wrong in ways a reader would notice -- a menu read as
// prose, a "next" link that leaves the book.
//
// Novel mirrors differ in markup but agree on shape: a heading, one block holding the
// chapter's paragraphs, and a link to what comes next. Each of those is found by the
// cheapest signal that works across sites, and anything below contentLenFloor is treated
// as "this page is not a chapter" rather than ingested.
type genericSite struct{ contentLenFloor int }

// Noise that is never chapter prose. Kept explicit: a substring match on "ad" would also
// strike "reader", and a site's own chapter container is often a plain <div>.
const genericNoiseSelector = "script, style, noscript, iframe, nav, header, footer, aside, form, button, select, textarea, svg"

// Containers worth scoring. A chapter's text lives in one of these on every site seen so
// far; scoring every element instead would rank <body> top on a page with no wrapper.
const genericCandidateSelector = "div, article, section, main, td"

// Link text that means "the next page of this same chapter" (the chapter continues) and
// "the next chapter". Matched case-insensitively against the anchor's own text.
var (
	genericNextPageWords    = []string{"下一页", "下一頁", "next page"}
	genericNextChapterWords = []string{"下一章", "下一節", "下一节", "next chapter", "next"}
)

func (g genericSite) FetchPage(
	ctx context.Context, client *httpClient, pageURL string,
) (Page, bool, error) {
	resp, err := client.Get(ctx, pageURL)
	if err != nil {
		return Page{}, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusGone {
		return Page{}, true, nil
	}
	if resp.StatusCode != http.StatusOK {
		return Page{}, false, fmt.Errorf("generic: unexpected status %d for %s", resp.StatusCode, pageURL)
	}
	page, err := parseGenericPage(resp.Body, pageURL, g.floor())
	if err != nil {
		return Page{}, false, err
	}
	return page, false, nil
}

func (g genericSite) floor() int {
	if g.contentLenFloor > 0 {
		return g.contentLenFloor
	}
	return 200
}

func parseGenericPage(body io.Reader, pageURL string, floor int) (Page, error) {
	doc, err := goquery.NewDocumentFromReader(body)
	if err != nil {
		return Page{}, fmt.Errorf("generic: parse %s: %w", pageURL, err)
	}
	doc.Find(genericNoiseSelector).Remove()

	container := densestTextBlock(doc, floor)
	if container == nil {
		return Page{}, fmt.Errorf("generic: no chapter text found on %s (under %d characters)", pageURL, floor)
	}
	paragraphs := chapterParagraphs(container)
	text := strings.Join(paragraphs, "\n\n")
	if len([]rune(text)) < floor {
		return Page{}, fmt.Errorf("generic: chapter text on %s is too short to be a chapter", pageURL)
	}

	nextURL, continues := nextLink(doc, pageURL)
	return Page{
		Title:     chapterTitle(doc, container),
		Text:      text,
		NextURL:   nextURL,
		Continues: continues,
	}, nil
}

// densestTextBlock picks the element holding the chapter. Only PROSE counts toward the
// score (see proseWeight): a page's longest block of text is often its chapter index, and
// on sites that print those titles as plain text rather than links, length and link
// density cannot tell the two apart. The deepest element that keeps nearly all of the
// winner's score wins, so an outer page wrapper loses to the chapter container itself.
func densestTextBlock(doc *goquery.Document, floor int) *goquery.Selection {
	var best *goquery.Selection
	bestScore := 0.0
	doc.Find(genericCandidateSelector).Each(func(_ int, sel *goquery.Selection) {
		score := proseWeight(sel, floor)
		if score > bestScore {
			best, bestScore = sel, score
		}
	})
	if best == nil {
		return nil
	}
	deepest := best
	best.Find(genericCandidateSelector).Each(func(_ int, sel *goquery.Selection) {
		if proseWeight(sel, floor) >= bestScore*0.95 && sel.Parents().Length() > deepest.Parents().Length() {
			deepest = sel
		}
	})
	return deepest
}

// Sentence-ending punctuation across scripts: Latin, CJK, Devanagari's danda, Khmer,
// Urdu, Armenian, Ethiopic. A chapter's paragraphs end sentences; a list of chapter
// titles, a menu or a byline block does not.
//
// Some scripts -- Thai and Lao among them -- end sentences with a space and nothing else,
// so punctuation can only ever be evidence FOR prose, never a requirement: see the length
// fallback in looksLikeProse.
const sentenceEnders = `.!?…。！？」』”"’'）)।॥។۔؟։።`

// proseLineRunes is the length at which a line stops looking like a label. Deliberately
// short, because a CJK sentence carries far more in a rune than a Latin one does.
const proseLineRunes = 24

// looksLikeProse is one line's verdict: long enough to be a sentence and punctuated like
// one, or long enough that it clearly is not a label.
func looksLikeProse(line string) bool {
	runes := []rune(line)
	if len(runes) < proseLineRunes {
		return false
	}
	if strings.ContainsRune(sentenceEnders, runes[len(runes)-1]) {
		return true
	}
	// An unpunctuated line is prose when it is long enough that no index would print it:
	// a wrapped paragraph, a line whose closing quote was stripped, or a script that ends
	// sentences without a mark. Set against index items, which are a title's length.
	return len(runes) >= 2*proseLineRunes
}

// repeatedStem reports whether most lines open the same way, which is what an index of
// chapter titles looks like and what prose never does.
func repeatedStem(lines []string) bool {
	const stem = 10
	if len(lines) < 4 {
		return false
	}
	counts := map[string]int{}
	for _, line := range lines {
		runes := []rune(line)
		if len(runes) < stem {
			continue
		}
		counts[string(runes[:stem])]++
	}
	for _, count := range counts {
		// A clear majority, not merely half: two paragraphs can legitimately open the
		// same way (a name, a piece of dialogue), a chapter index repeats its stem
		// down the whole column.
		if count >= 4 && count*10 >= len(lines)*6 {
			return true
		}
	}
	return false
}

// proseWeight is how much of this block reads as chapter prose. Zero means "not a
// chapter": too little of it, too much of it inside links, or lines that repeat a stem
// the way an index does.
func proseWeight(sel *goquery.Selection, floor int) float64 {
	lines := chapterParagraphs(sel)
	if len(lines) == 0 || repeatedStem(lines) {
		return 0
	}
	total, prose, proseCount := 0, 0, 0
	for _, line := range lines {
		length := len([]rune(line))
		total += length
		if looksLikeProse(line) {
			prose += length
			proseCount++
		}
	}
	if total == 0 || proseCount < 2 || prose < floor {
		return 0
	}
	linkLength := 0
	sel.Find("a").Each(func(_ int, a *goquery.Selection) {
		linkLength += len([]rune(strings.TrimSpace(a.Text())))
	})
	density := float64(linkLength) / float64(total)
	if density > 0.3 { // navigation dressed up as text
		return 0
	}
	return float64(prose) * (1 - density)
}

// chapterParagraphs reads the container's paragraphs: <p> elements when the site uses
// them, otherwise the <br>-separated lines that sites without <p> use instead.
func chapterParagraphs(container *goquery.Selection) []string {
	var paragraphs []string
	container.Find("p").Each(func(_ int, p *goquery.Selection) {
		if text := normalizeSpace(p.Text()); text != "" {
			paragraphs = append(paragraphs, text)
		}
	})
	if len(paragraphs) >= 2 {
		return paragraphs
	}
	var lines []string
	for _, line := range strings.Split(lineBrokenText(container), "\n") {
		if text := normalizeSpace(line); text != "" {
			lines = append(lines, text)
		}
	}
	if len(lines) > len(paragraphs) {
		return lines
	}
	return paragraphs
}

// lineBrokenText renders the node's text with <br> and block elements as line breaks,
// which goquery's Text() alone does not do -- without it a <br>-separated chapter comes
// out as one run-on paragraph.
func lineBrokenText(sel *goquery.Selection) string {
	var out strings.Builder
	var walk func(*html.Node)
	walk = func(node *html.Node) {
		switch {
		case node.Type == html.TextNode:
			out.WriteString(node.Data)
		case node.Type == html.ElementNode && (node.Data == "br" || node.Data == "p" || node.Data == "div"):
			out.WriteString("\n")
		}
		for child := node.FirstChild; child != nil; child = child.NextSibling {
			walk(child)
		}
		if node.Type == html.ElementNode && (node.Data == "p" || node.Data == "div") {
			out.WriteString("\n")
		}
	}
	for _, node := range sel.Nodes {
		walk(node)
	}
	return out.String()
}

// chapterTitle is inert metadata (source_meta.site_chapter_no) and never parsed for
// numbering (§3.1), so a heading that carries the site's name is a cosmetic problem only.
func chapterTitle(doc *goquery.Document, container *goquery.Selection) string {
	for _, selector := range []string{"h1", "h2", "h3", "h4"} {
		if title := normalizeSpace(container.Find(selector).First().Text()); title != "" {
			return title
		}
	}
	for _, selector := range []string{"h1", "h2", "title"} {
		if title := normalizeSpace(doc.Find(selector).First().Text()); title != "" {
			return title
		}
	}
	return ""
}

// nextLink finds where to go next, and whether that is the same chapter continued. A
// next-page link wins over a next-chapter link: sites that paginate show both, and
// following the chapter link first would skip the rest of the chapter being read.
func nextLink(doc *goquery.Document, pageURL string) (string, bool) {
	if href, ok := firstHref(doc.Find(`a[rel="next"]`)); ok {
		return resolveURL(pageURL, href), false
	}
	var pageHref, chapterHref, attrHref string
	doc.Find("a[href]").EachWithBreak(func(_ int, a *goquery.Selection) bool {
		text := strings.ToLower(normalizeSpace(a.Text()))
		href, _ := a.Attr("href")
		switch {
		case pageHref == "" && matchesAny(text, genericNextPageWords):
			pageHref = href
		case chapterHref == "" && matchesAny(text, genericNextChapterWords):
			chapterHref = href
		case attrHref == "" && hasNextAttr(a):
			attrHref = href
		}
		return pageHref == "" // a next-page link settles it
	})
	switch {
	case pageHref != "":
		return resolveURL(pageURL, pageHref), true
	case chapterHref != "":
		return resolveURL(pageURL, chapterHref), false
	case attrHref != "":
		return resolveURL(pageURL, attrHref), false
	}
	return "", false
}

// hasNextAttr matches id/class tokens like "next", "next_url" or "chapter-next" without
// matching a word that merely contains them ("context").
func hasNextAttr(a *goquery.Selection) bool {
	id, _ := a.Attr("id")
	class, _ := a.Attr("class")
	for _, token := range strings.FieldsFunc(id+" "+class, func(r rune) bool {
		return r == ' ' || r == '-' || r == '_'
	}) {
		if strings.EqualFold(token, "next") {
			return true
		}
	}
	return false
}

func matchesAny(text string, words []string) bool {
	for _, word := range words {
		if strings.Contains(text, word) {
			return true
		}
	}
	return false
}

func firstHref(sel *goquery.Selection) (string, bool) {
	return sel.First().Attr("href")
}

func normalizeSpace(text string) string {
	return strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(text, " ", " "), "　", " "))
}

// cjkShare is the fraction of letters that are Han characters, which is how a preview
// tells a source-language page from an already-translated one.
func cjkShare(text string) float64 {
	letters, han := 0, 0
	for _, r := range text {
		if !unicode.IsLetter(r) {
			continue
		}
		letters++
		if unicode.Is(unicode.Han, r) {
			han++
		}
	}
	if letters == 0 {
		return 0
	}
	return float64(han) / float64(letters)
}
