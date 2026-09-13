package main

import (
	"strings"
	"testing"
)

func TestNovel543TitleAndContinuation(t *testing.T) {
	tests := []struct {
		input, title string
		continues    bool
	}{
		{" 第4687章 秩序之神！ (1/2) ", "第4687章 秩序之神！", true},
		{"第4687章 秩序之神！ (2/2)", "第4687章 秩序之神！", false},
		{"第4688章 多高才算高啊！", "第4688章 多高才算高啊！", false},
	}
	for _, test := range tests {
		title, continues := novel543TitleAndContinuation(test.input)
		if title != test.title || continues != test.continues {
			t.Errorf("novel543TitleAndContinuation(%q) = (%q, %v), want (%q, %v)",
				test.input, title, continues, test.title, test.continues)
		}
	}
}

func TestNovel543Boilerplate(t *testing.T) {
	if !isNovel543Boilerplate("溫馨提示: 登錄用戶跨設備永久保存書架的數據") {
		t.Fatal("expected reader prompt to be removed")
	}
	if isNovel543Boilerplate("他收到溫馨提示後，便離開了。") {
		t.Fatal("ordinary story text must survive")
	}
}

func TestParseNovel543Page(t *testing.T) {
	html := `<div id="chapterWarp"><div class="chapter-content">
		<h1>第4687章 秩序之神！ (1/2)</h1>
		<div class="content"><p>第一段。</p><div class="gadBlock">ad</div>
		<p>溫馨提示: 登錄用戶跨設備永久保存書架的數據</p><p>第二段。</p></div>
	</div></div><div class="foot-nav"><a href="/02051597/8095_4690_2.html">下一章</a></div>`

	page, err := parseNovel543Page(strings.NewReader(html),
		"https://www.novel543.com/02051597/8095_4690.html")
	if err != nil {
		t.Fatal(err)
	}
	if page.Title != "第4687章 秩序之神！" || !page.Continues {
		t.Fatalf("unexpected title/continuation: %#v", page)
	}
	if page.Text != "第一段。\n\n第二段。" {
		t.Fatalf("unexpected content: %q", page.Text)
	}
	if page.NextURL != "https://www.novel543.com/02051597/8095_4690_2.html" {
		t.Fatalf("unexpected next URL: %q", page.NextURL)
	}
}
