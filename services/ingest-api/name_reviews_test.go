package main

import (
	"context"
	"testing"
)

func TestRenderingForRole(t *testing.T) {
	tests := []struct{ role, class, method string }{
		{"chinese_person", "character_name", "pinyin"},
		{"foreign_person", "character_name", "restored_name"},
		{"personal_title", "character_name", "translated_title"},
		{"semantic_term", "semantic_term", "semantic_translation"},
	}
	for _, tt := range tests {
		class, method, ok := renderingForRole(tt.role)
		if !ok || class != tt.class || method != tt.method {
			t.Fatalf("renderingForRole(%q) = %q,%q,%v", tt.role, class, method, ok)
		}
	}
	if _, _, ok := renderingForRole("character"); ok {
		t.Fatal("unknown role was accepted")
	}
}

func TestApproveReviewPublishesSelectedConstraintClass(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	novelID := seedNovelWithGlossary(t, store, nil)
	seed := func(source string) {
		if _, err := store.db.Exec(ctx, `INSERT INTO character_name_review
			(novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason)
			VALUES($1,$2,1,'sha256:test',0,3,$2,'[]','test')`, novelID, source); err != nil {
			t.Fatalf("seed review: %v", err)
		}
	}
	seed("星源兽")
	version, queue, err := store.ApproveCharacterName(ctx, novelID, "星源兽", "Star Source Beast", "semantic_term", "tester")
	if err != nil || version != 1 || len(queue) != 0 {
		t.Fatalf("semantic approval: version=%d queue=%v err=%v", version, queue, err)
	}
	var class, role, method string
	if err := store.db.QueryRow(ctx, `SELECT g.constraint_class,r.term_role,r.rendering_method
		FROM glossary g JOIN character_name_review r USING(novel_id,source_term)
		WHERE g.novel_id=$1 AND g.source_term='星源兽'`, novelID).Scan(&class, &role, &method); err != nil {
		t.Fatal(err)
	}
	if class != "semantic_term" || role != "semantic_term" || method != "semantic_translation" {
		t.Fatalf("semantic fields = %q,%q,%q", class, role, method)
	}

	seed("契科夫")
	version, _, err = store.ApproveCharacterName(ctx, novelID, "契科夫", "Chekhov", "foreign_person", "tester")
	if err != nil || version != 2 {
		t.Fatalf("foreign approval: version=%d err=%v", version, err)
	}
	if err := store.db.QueryRow(ctx, `SELECT g.constraint_class,r.term_role,r.rendering_method
		FROM glossary g JOIN character_name_review r USING(novel_id,source_term)
		WHERE g.novel_id=$1 AND g.source_term='契科夫'`, novelID).Scan(&class, &role, &method); err != nil {
		t.Fatal(err)
	}
	if class != "character_name" || role != "foreign_person" || method != "restored_name" {
		t.Fatalf("foreign fields = %q,%q,%q", class, role, method)
	}
}
