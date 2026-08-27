package main

import "testing"

// The randomized "continue to next page" nag is the reason content-hash dedup silently
// failed on re-scrapes (see shuhaigeBoilerplate's comment): the same page served a
// different wording each fetch, so identical chapters hashed differently. Each variant
// below was observed on real fetched pages this session.
func TestIsShuhaigeBoilerplateCatchesEveryNagVariant(t *testing.T) {
	boilerplate := []string{
		"小主，这个章节后面还有哦，请点击下一页继续阅读，后面更精彩！",
		"本小章还未完，请点击下一页继续阅读后面精彩内容！",
		"这章没有结束，请点击下一页继续阅读！",
		"喜欢混沌天帝诀请大家收藏：(m.shuhaige.net)混沌天帝诀书海阁小说网更新速度全网最快。",
		"(本章完)",
	}
	for _, text := range boilerplate {
		if !isShuhaigeBoilerplate(text) {
			t.Errorf("isShuhaigeBoilerplate(%q) = false, want true", text)
		}
	}
}

func TestIsShuhaigeBoilerplateKeepsStoryText(t *testing.T) {
	// Ordinary narration and dialogue must survive — an over-broad match here silently
	// deletes story text, which is far worse than leaving an ad in.
	story := []string{
		"他缓缓睁开了眼睛。",
		"“你是谁？”少年问道。",
		"下一页的风景，他从未见过。", // contains 下一页 but is not the pager nag
	}
	for _, text := range story {
		if isShuhaigeBoilerplate(text) {
			t.Errorf("isShuhaigeBoilerplate(%q) = true, want false", text)
		}
	}
}
