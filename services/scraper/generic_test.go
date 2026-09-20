package main

import (
	"fmt"
	"strings"
	"testing"
)

// Synthetic pages, not captures of any real site: each one isolates a shape the generic
// reader has to handle (paragraphs vs <br> lines, a menu competing with the chapter, a
// paginated chapter) so a failure names the shape that broke.

// Varied sentences, because real chapter prose is varied: a block of near-identical
// lines is what an index looks like, and the reader is meant to reject that.
var proseLines = []string{
	"The lantern guttered twice before the door finally opened, and nobody inside moved to greet the visitor.",
	"Rain had been falling since the third watch, and the courtyard stones were slick enough to send a careless guard sprawling.",
	"She counted the seals on the letter again, hoping the number would change, and it did not.",
	"Somewhere below the terrace a bell rang once, which meant the gates were closed for the night.",
	"He had been told the road would be empty at this hour, and whoever told him that had never walked it.",
}

func prose(paragraphs int) string {
	var out strings.Builder
	for i := 0; i < paragraphs; i++ {
		out.WriteString("<p>" + proseLines[i%len(proseLines)] + "</p>")
	}
	return out.String()
}

func TestGenericReaderTakesTheChapterOverTheSurroundingPage(t *testing.T) {
	page, err := parseGenericPage(strings.NewReader(`<html><body>
		<nav><a href="/">Home</a><a href="/list">Chapters</a><a href="/top">Rankings</a></nav>
		<div id="wrapper">
		  <div class="sidebar"><a href="/c/1">Chapter 1</a><a href="/c/2">Chapter 2</a><a href="/c/3">Chapter 3</a></div>
		  <div class="content"><h1>Chapter 7: The Locked Gate</h1>`+prose(4)+`</div>
		</div>
		<div class="foot"><a href="/c/6">Previous</a><a href="/c/8">Next Chapter</a></div>
		</body></html>`), "https://example.test/c/7", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if page.Title != "Chapter 7: The Locked Gate" {
		t.Errorf("title = %q", page.Title)
	}
	if strings.Contains(page.Text, "Rankings") || strings.Contains(page.Text, "Chapter 2") {
		t.Errorf("navigation leaked into the chapter: %q", page.Text)
	}
	if got := strings.Count(page.Text, "\n\n"); got != 3 {
		t.Errorf("want 4 paragraphs, got %d separators in %q", got, page.Text)
	}
	if page.NextURL != "https://example.test/c/8" || page.Continues {
		t.Errorf("next = %q continues=%v", page.NextURL, page.Continues)
	}
}

func TestGenericReaderSplitsBreakSeparatedChapters(t *testing.T) {
	lines := []string{
		"他抬頭看向夜空，什麼也沒有說，只是把手裡的燈籠又提高了一些。",
		"院子裡的石板被雨水洗得發亮，走在上面要格外小心才不會滑倒。",
		"遠處的鐘聲響了一下，那表示城門已經關上了，今夜誰也進不來。",
		"她把那封信上的印記數了一遍又一遍，數字始終沒有變過。",
		"有人說這條路這個時辰不會有人，說這話的人顯然沒有走過。",
		"他終於把燈籠放下，靠著門框坐了下來，什麼也不想再說了。",
	}
	page, err := parseGenericPage(strings.NewReader(`<html><body><div id="content">`+
		strings.Join(lines, "<br><br>")+`</div>
		<a id="next_url" href="page2.html">下一页</a></body></html>`), "https://example.test/read/5", 100)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := strings.Count(page.Text, "\n\n"); got != 5 {
		t.Errorf("want 6 lines separated by blank lines, got %d separators in %q", got, page.Text)
	}
	// A next-PAGE link continues the same chapter; the walker assembles it (§3.1).
	if page.NextURL != "https://example.test/read/page2.html" || !page.Continues {
		t.Errorf("next = %q continues=%v", page.NextURL, page.Continues)
	}
}

func TestGenericReaderPrefersTheNextPageLinkOverTheNextChapterLink(t *testing.T) {
	page, err := parseGenericPage(strings.NewReader(`<html><body>
		<div class="read">`+prose(3)+`</div>
		<div class="nav"><a href="/c/9">下一章</a><a href="/c/8_2">下一页</a></div>
		</body></html>`), "https://example.test/c/8", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if page.NextURL != "https://example.test/c/8_2" || !page.Continues {
		t.Fatalf("a paginated chapter must finish before the next one: %q continues=%v", page.NextURL, page.Continues)
	}
}

func TestGenericReaderUsesRelNextAndIdWhenTheLinkTextIsAnIcon(t *testing.T) {
	page, err := parseGenericPage(strings.NewReader(`<html><body><article>`+prose(3)+`</article>
		<a rel="next" href="/c/12">›</a></body></html>`), "https://example.test/c/11", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if page.NextURL != "https://example.test/c/12" {
		t.Errorf("rel=next ignored: %q", page.NextURL)
	}
	attr, err := parseGenericPage(strings.NewReader(`<html><body><article>`+prose(3)+`</article>
		<a class="btn next-chapter" href="/c/13">»</a></body></html>`), "https://example.test/c/12", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if attr.NextURL != "https://example.test/c/13" {
		t.Errorf("next-chapter class ignored: %q", attr.NextURL)
	}
}

// "context" contains "next"; a word that merely contains it is not a next link.
func TestGenericReaderIgnoresWordsThatMerelyContainNext(t *testing.T) {
	page, err := parseGenericPage(strings.NewReader(`<html><body><article>`+prose(3)+`</article>
		<a class="context-help" href="/help">About this context</a></body></html>`), "https://example.test/c/1", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if page.NextURL != "" {
		t.Errorf("want no next link, got %q", page.NextURL)
	}
}

// A page with no chapter on it (a list, an interstitial, a stub) must stop the walk
// rather than ingest whatever text it does have.
func TestGenericReaderRefusesAPageThatIsNotAChapter(t *testing.T) {
	_, err := parseGenericPage(strings.NewReader(`<html><body>
		<div class="list"><a href="/c/1">Chapter 1</a><a href="/c/2">Chapter 2</a><a href="/c/3">Chapter 3</a></div>
		<div class="notice">Please log in to continue reading.</div></body></html>`), "https://example.test/list", 200)
	if err == nil {
		t.Fatal("a chapter list must not read as a chapter")
	}
}

// The failure this reader hit in the wild: a site prints its chapter index as plain TEXT,
// not links, so link density sees nothing wrong with it. It is longer than the chapter
// itself, and every line repeats the book's name -- which is what marks it as an index.
func titleIndex(book string, from, count int) string {
	var out strings.Builder
	for i := 0; i < count; i++ {
		out.WriteString(fmt.Sprintf("<div class=\"chp-item\">%s Chapter %d</div>", book, from+i))
	}
	return out.String()
}

func TestGenericReaderRefusesAPlainTextChapterIndex(t *testing.T) {
	_, err := parseGenericPage(strings.NewReader(`<html><body>
		<div class="chapter-list">`+titleIndex("Taming Master", 100, 40)+`</div>
		</body></html>`), "https://example.test/c/128", 200)
	if err == nil {
		t.Fatal("a list of chapter titles must not read as a chapter")
	}
}

func TestGenericReaderTakesTheChapterOverALongerTitleIndex(t *testing.T) {
	page, err := parseGenericPage(strings.NewReader(`<html><body>
		<div class="chapter-list">`+titleIndex("Taming Master", 100, 40)+`</div>
		<div class="chapter-entity"><h1>Taming Master Chapter 128</h1>`+prose(5)+`</div>
		</body></html>`), "https://example.test/c/128", 200)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if strings.Contains(page.Text, "Chapter 101") {
		t.Errorf("the index was read as the chapter: %q", page.Text)
	}
	if got := strings.Count(page.Text, "\n\n"); got != 4 {
		t.Errorf("want the 5 prose paragraphs, got %d separators in %q", got, page.Text)
	}
}

// Thai ends sentences with a space, not a mark, so punctuation cannot be a requirement
// for prose -- only evidence of it. These lines carry no sentence-ending punctuation at
// all and must still read as a chapter.
func TestGenericReaderReadsScriptsThatEndSentencesWithoutPunctuation(t *testing.T) {
	lines := []string{
		"เขาเงยหน้าขึ้นมองท้องฟ้ายามค่ำคืนอยู่ครู่หนึ่งแล้วก็ไม่ได้พูดอะไรออกมาอีกเลย",
		"ฝนตกลงมาตั้งแต่ยามดึกทำให้พื้นหินในลานบ้านลื่นจนต้องเดินอย่างระมัดระวัง",
		"เสียงระฆังจากที่ไกลออกไปดังขึ้นหนึ่งครั้งซึ่งหมายความว่าประตูเมืองปิดแล้ว",
		"เธอนับตราประทับบนจดหมายฉบับนั้นซ้ำอีกครั้งแต่จำนวนก็ยังคงไม่เปลี่ยนไป",
	}
	page, err := parseGenericPage(strings.NewReader(`<html><body><div id="content"><p>`+
		strings.Join(lines, "</p><p>")+`</p></div></body></html>`), "https://example.test/c/1", 200)
	if err != nil {
		t.Fatalf("unpunctuated prose was refused: %v", err)
	}
	if got := strings.Count(page.Text, "\n\n"); got != 3 {
		t.Errorf("want 4 paragraphs, got %d separators in %q", got, page.Text)
	}
}

func TestCjkShareTellsSourceTextFromTranslatedText(t *testing.T) {
	if share := cjkShare("他抬頭看向夜空。"); share < 0.9 {
		t.Errorf("source text share = %v", share)
	}
	if share := cjkShare("He looked up at the night sky."); share != 0 {
		t.Errorf("translated text share = %v", share)
	}
}
