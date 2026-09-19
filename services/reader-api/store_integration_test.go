package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type integrationFixture struct {
	novelID      string
	otherNovelID string
}

func integrationDatabase(t *testing.T) (*Store, *pgxpool.Pool) {
	t.Helper()
	databaseURL := os.Getenv("READER_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("READER_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	admin, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatalf("admin pool: %v", err)
	}
	if err := admin.Ping(ctx); err != nil {
		admin.Close()
		t.Fatalf("admin database: %v", err)
	}
	var migrationReady bool
	if err := admin.QueryRow(ctx,
		`SELECT to_regclass('public.reader_progress') IS NOT NULL`,
	).Scan(&migrationReady); err != nil || !migrationReady {
		admin.Close()
		t.Fatalf("database must have migrations through 0007 applied")
	}
	// GetChapter (object-store reads) and scrape-job creation (Redis) aren't exercised by
	// this integration suite, so nil clients are fine here — adding MinIO/Redis as
	// dependencies of this test harness is out of scope; see handlers_test.go's fakeStore
	// for that coverage.
	store, err := newStore(ctx, Config{
		ReaderDatabaseURL: databaseURL, ProgressDatabaseURL: databaseURL,
	}, nil, nil)
	if err != nil {
		admin.Close()
		t.Fatalf("reader store: %v", err)
	}
	t.Cleanup(store.Close)
	t.Cleanup(admin.Close)
	return store, admin
}

// One novel with three chapters and a second novel, so a query that forgets its novel or
// chapter predicate shows up as a failure.
func seedIntegrationFixture(t *testing.T, admin *pgxpool.Pool) integrationFixture {
	t.Helper()
	ctx := context.Background()
	fixture := integrationFixture{novelID: uuid.NewString(), otherNovelID: uuid.NewString()}
	for _, novelID := range []string{fixture.novelID, fixture.otherNovelID} {
		if _, err := admin.Exec(ctx,
			`INSERT INTO novel (id, title, source_lang, target_lang, ontology)
			 VALUES ($1, 'Reader Fixture', 'zh', 'en', '{"kinds":["character","group"]}'::jsonb)`,
			novelID); err != nil {
			t.Fatalf("seed novel: %v", err)
		}
	}
	for chapter := 1; chapter <= 3; chapter++ {
		if _, err := admin.Exec(ctx,
			// raw_hash is the content-dedup key and is unique per novel, so each chapter
			// needs its own.
			`INSERT INTO chapter (novel_id, chapter_index, raw_uri, raw_hash, source_meta, translation_ready)
			 VALUES ($1, $2, 'raw://c', $3, '{}'::jsonb, true)`,
			fixture.novelID, chapter, fmt.Sprintf("hash-%d", chapter)); err != nil {
			t.Fatalf("seed chapter: %v", err)
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO mention_span (novel_id, chapter_index, char_start, char_end) VALUES ($1, $2, 0, 4)`,
			fixture.novelID, chapter); err != nil {
			t.Fatalf("seed span: %v", err)
		}
	}
	t.Cleanup(func() {
		for _, novelID := range []string{fixture.novelID, fixture.otherNovelID} {
			_, _ = admin.Exec(context.Background(), `DELETE FROM novel WHERE id = $1`, novelID)
		}
	})
	return fixture
}

func setProgress(t *testing.T, admin *pgxpool.Pool, novelID string, chapter int) {
	t.Helper()
	if _, err := admin.Exec(context.Background(),
		`INSERT INTO reader_progress (novel_id, reader_id, current_chapter)
		 VALUES ($1, 'reader-a', $2)
		 ON CONFLICT (novel_id, reader_id) DO UPDATE SET current_chapter=EXCLUDED.current_chapter`,
		novelID, chapter); err != nil {
		t.Fatalf("set progress: %v", err)
	}
}

func TestListNovelsScopesProgressToReader(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 2)

	novels, err := store.ListNovels(context.Background(), "reader-a")
	if err != nil {
		t.Fatalf("list novels: %v", err)
	}
	progress := make(map[string]int, len(novels))
	for _, novel := range novels {
		progress[novel.ID] = novel.CurrentChapter
	}
	if progress[fixture.novelID] != 2 {
		t.Fatalf("reader progress = %d, want 2", progress[fixture.novelID])
	}
	if progress[fixture.otherNovelID] != 0 {
		t.Fatalf("unread book progress = %d, want 0", progress[fixture.otherNovelID])
	}

	withoutReader, err := store.ListNovels(context.Background(), "")
	if err != nil {
		t.Fatalf("list novels without reader: %v", err)
	}
	for _, novel := range withoutReader {
		if novel.CurrentChapter != 0 {
			t.Fatalf("anonymous list exposed progress for %s: %d", novel.ID, novel.CurrentChapter)
		}
	}
}

// A reader at chapter 1 gets chapter 1's facts status and spans, and nothing later.
func TestSpoilerGateEndToEnd(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := admin.Exec(ctx, `UPDATE chapter SET facts_count=4 WHERE novel_id=$1 AND chapter_index=1`,
		fixture.novelID); err != nil {
		t.Fatal(err)
	}
	status, err := store.GetFactsStatus(ctx, fixture.novelID, 1, 1)
	if err != nil || status.State != "ready" || status.FactsCount == nil || *status.FactsCount != 4 {
		t.Fatalf("chapter 1 status = %+v, %v", status, err)
	}
	if status, err := store.GetFactsStatus(ctx, fixture.novelID, 2, 2); err != nil || status.State != "pending" {
		t.Fatalf("chapter 2 status = %+v, %v; want pending", status, err)
	}
	if _, err := store.GetFactsStatus(ctx, fixture.novelID, 2, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("chapter 2 at 1 must be not found, got %v", err)
	}
	if err := store.withReaderTx(ctx, fixture.novelID, 1, func(tx pgx.Tx) error {
		for chapter, want := range map[int]int{1: 1, 2: 0} {
			spans, err := chapterSpansInTx(ctx, tx, fixture.novelID, chapter)
			if err != nil {
				return err
			}
			if len(spans) != want {
				t.Fatalf("chapter %d at 1: %d spans, want %d", chapter, len(spans), want)
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

// RLS is the second lock: with no GUCs set, the reader role sees nothing at all, and a
// query for another novel returns nothing even inside a valid reader transaction.
func TestRLSAloneFailsClosedAndDoesNotLeakSettings(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()

	conn, err := store.readerDB.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer conn.Release()
	for _, table := range []string{"mention_span", "chapter_fact", "subject", "chunk"} {
		var count int
		if err := conn.QueryRow(ctx, fmt.Sprintf("SELECT count(*) FROM %s", table)).Scan(&count); err != nil {
			t.Fatalf("count %s without GUCs: %v", table, err)
		}
		if count != 0 {
			t.Fatalf("%s returned %d rows with no reader GUCs set", table, count)
		}
	}

	// Right transaction, wrong novel: the other novel's id must not widen the gate.
	if err := store.withReaderTx(ctx, fixture.otherNovelID, 3, func(tx pgx.Tx) error {
		spans, err := chapterSpansInTx(ctx, tx, fixture.novelID, 1)
		if err != nil {
			return err
		}
		if len(spans) != 0 {
			t.Fatalf("another novel's gate exposed %d spans", len(spans))
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestDatabaseRolesAreLeastPrivilege(t *testing.T) {
	_, admin := integrationDatabase(t)
	ctx := context.Background()
	for _, table := range []string{"chapter_fact", "subject", "mention_span", "chunk", "glossary"} {
		for _, privilege := range []string{"INSERT", "UPDATE", "DELETE"} {
			var granted bool
			if err := admin.QueryRow(ctx,
				`SELECT has_table_privilege('rls_reader', $1, $2)`, table, privilege).Scan(&granted); err != nil {
				t.Fatalf("privilege check %s %s: %v", table, privilege, err)
			}
			if granted {
				t.Fatalf("rls_reader may %s %s", privilege, table)
			}
		}
	}
}

func TestFactsStatusReportsSafeFailureWithinReaderGate(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := admin.Exec(ctx, `UPDATE chapter SET enrichment_attempts=1,
   enrichment_retry_at=now()+interval '5 minutes' WHERE novel_id=$1 AND chapter_index=1`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO chapter_failure (novel_id,chapter_index,stage,error_type,error_code)
   VALUES ($1,1,'facts','ProviderResponseError','provider_invalid_json'),
          ($1,1,'facts','ProviderResponseError','some_unlisted_code')`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	status, err := store.GetFactsStatus(ctx, fixture.novelID, 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	// The newest failure is an unlisted code: it must reach the reader only as a bounded category.
	if status.State != "processing" || status.RetryAt == nil ||
		status.RetryCategory == nil || *status.RetryCategory != "stage_failed" {
		t.Fatalf("wrong retry status: %+v", status)
	}
}

func TestChapterRenderingExposesOneProvisionalChoiceAndRespectsGate(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := admin.Exec(ctx, `INSERT INTO character_name_review
  (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason,term_role,rendering_method)
  VALUES ($1,'凌峰',1,'hash-1',0,2,'凌峰来了。','[{"target_term":"Ling Feng","pronunciation":[],"segmentation":"surname+given","method":"pinyin"},{"target_term":"Other","pronunciation":[],"segmentation":"","method":"pinyin"}]','provisional','chinese_person','pinyin')`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO term_rendering_occurrence
  (novel_id,chapter_index,char_start,char_end,source_term,display_term,method)
  VALUES ($1,1,0,8,'凌峰','Lingfeng','aligned')`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = admin.Exec(ctx, `DELETE FROM character_name_review WHERE novel_id=$1`, fixture.novelID) })
	check := func(want bool) {
		t.Helper()
		if err := store.withReaderTx(ctx, fixture.novelID, 1, func(tx pgx.Tx) error {
			spans := []SpanView{{CharStart: 0, CharEnd: 8}}
			if err := attachChapterRenderings(ctx, tx, fixture.novelID, 1, 1, "Lingfeng came.", spans); err != nil {
				return err
			}
			r := spans[0].Rendering
			if want && (r == nil || r.Status != "pending" || r.TargetTerm == nil || *r.TargetTerm != "Ling Feng" || len(r.Candidates) != 1) {
				t.Fatalf("wrong provisional rendering: %+v", r)
			}
			if !want && r != nil && r.TargetTerm != nil {
				t.Fatalf("future choice leaked: %+v", r)
			}
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	}
	check(true)
	if _, err := admin.Exec(ctx, `UPDATE character_name_review SET first_seen_chapter=3 WHERE novel_id=$1`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	check(false)
}

// A character page is assembled from tagged facts up to the reader's chapter: never a
// later fact, never a character met later, nothing from another novel, and the name is
// filled in with its current spelling.
func TestWikiPageIsGatedAtTheReadersChapter(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	var hero string
	if err := admin.QueryRow(ctx, `INSERT INTO subject (novel_id, source_term, first_seen_chapter, kind)
		VALUES ($1, 'hero-source', 1, 'character') RETURNING id::text`, fixture.novelID).Scan(&hero); err != nil {
		t.Fatalf("seed character: %v", err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter)
		VALUES ($1, 'hero-source', 'Hero', 99, 0)`, fixture.novelID); err != nil {
		t.Fatalf("seed spelling: %v", err)
	}
	for _, row := range []struct {
		chapter int
		text    string
	}{{1, "⟦" + hero + "⟧ left home."}, {3, "⟦" + hero + "⟧ came back."}} {
		if _, err := admin.Exec(ctx, `INSERT INTO chapter_fact
			(novel_id, chapter_index, prompt_version, ordinal, text, category, subjects, source_hash, requested_model)
			VALUES ($1, $2, 'tagged-facts-v1.txt', 0, $3, 'event', ARRAY[$4::uuid], 'h', 'm')`,
			fixture.novelID, row.chapter, row.text, hero); err != nil {
			t.Fatalf("seed fact: %v", err)
		}
	}

	if pages, err := store.ListWikiPages(ctx, fixture.novelID, 0); err != nil || len(pages) != 0 {
		t.Fatalf("chapter 0 pages = %+v, %v; want none", pages, err)
	}
	page, err := store.GetWikiPage(ctx, fixture.novelID, hero, 2)
	if err != nil || page.Title != "Hero" || len(page.Facts) != 1 || page.Facts[0].Text != "Hero left home." {
		t.Fatalf("chapter 2 page = %+v, %v; want only the chapter 1 fact, named", page, err)
	}
	pages, err := store.ListWikiPages(ctx, fixture.novelID, 3)
	if err != nil || len(pages) != 1 || pages[0].Facts != 2 {
		t.Fatalf("chapter 3 pages = %+v, %v; want one character with two facts", pages, err)
	}
	if _, err := store.GetWikiPage(ctx, fixture.otherNovelID, hero, 99); !errors.Is(err, ErrNotFound) {
		t.Fatalf("another novel's reader saw the page: %v", err)
	}
}

// A retracted fact drops out of pages and counts; the fact row itself stays. A reader
// can only reach (and so retract) a fact from a chapter they have read.
func TestRetractedFactLeavesTheWiki(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	var hero string
	if err := admin.QueryRow(ctx, `INSERT INTO subject (novel_id, source_term, first_seen_chapter, kind)
		VALUES ($1, 'hero-source', 1, 'character') RETURNING id::text`, fixture.novelID).Scan(&hero); err != nil {
		t.Fatalf("seed character: %v", err)
	}
	for ordinal, text := range []string{"kept", "retracted"} {
		if _, err := admin.Exec(ctx, `INSERT INTO chapter_fact
			(novel_id, chapter_index, prompt_version, ordinal, text, category, subjects, source_hash, requested_model)
			VALUES ($1, 1, 'tagged-facts-v2.txt', $2, $3, 'event', ARRAY[$4::uuid], 'h', 'm')`,
			fixture.novelID, ordinal, text, hero); err != nil {
			t.Fatalf("seed fact: %v", err)
		}
	}
	if visible, err := store.FactVisible(ctx, fixture.novelID, 1, "tagged-facts-v2.txt", 1, 0); err != nil || visible {
		t.Fatalf("a chapter-1 fact was visible at chapter 0: %v, %v", visible, err)
	}
	if visible, err := store.FactVisible(ctx, fixture.novelID, 1, "tagged-facts-v2.txt", 1, 1); err != nil || !visible {
		t.Fatalf("a chapter-1 fact was not visible at chapter 1: %v, %v", visible, err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO fact_retraction (novel_id, chapter_index, prompt_version, ordinal, retracted_by)
		VALUES ($1, 1, 'tagged-facts-v2.txt', 1, 'reader')`, fixture.novelID); err != nil {
		t.Fatalf("retract: %v", err)
	}
	page, err := store.GetWikiPage(ctx, fixture.novelID, hero, 1)
	if err != nil || len(page.Facts) != 1 || page.Facts[0].Text != "kept" || page.Facts[0].Ordinal != 0 {
		t.Fatalf("page = %+v, %v; want only the kept fact", page, err)
	}
	pages, err := store.ListWikiPages(ctx, fixture.novelID, 1)
	if err != nil || len(pages) != 1 || pages[0].Facts != 1 {
		t.Fatalf("pages = %+v, %v; want a count of one", pages, err)
	}
}

// Organizations, places and items get pages like characters, each with its kind, and the
// Events timeline holds only event facts up to the reader's chapter, names filled in.
func TestWikiKindsAndEventsTimeline(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	ids := map[string]string{}
	for _, s := range []struct{ term, kind string }{{"hero-source", "character"}, {"sect-source", "organization"}} {
		var id string
		if err := admin.QueryRow(ctx, `INSERT INTO subject (novel_id, source_term, first_seen_chapter, kind)
			VALUES ($1, $2, 1, $3) RETURNING id::text`, fixture.novelID, s.term, s.kind).Scan(&id); err != nil {
			t.Fatalf("seed subject: %v", err)
		}
		ids[s.term] = id
	}
	if _, err := admin.Exec(ctx, `INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter)
		VALUES ($1, 'hero-source', 'Hero', 98, 0), ($1, 'sect-source', 'Cloud Gate', 99, 0)`, fixture.novelID); err != nil {
		t.Fatalf("seed spellings: %v", err)
	}
	hero, sect := ids["hero-source"], ids["sect-source"]
	for i, row := range []struct {
		chapter        int
		category, text string
		subjects       []string
	}{
		{1, "affiliation", "⟦" + hero + "⟧ joined the ⟦" + sect + "⟧.", []string{hero, sect}},
		{1, "event", "The ⟦" + sect + "⟧ was founded.", []string{sect}},
		{3, "event", "⟦" + hero + "⟧ left the ⟦" + sect + "⟧.", []string{hero, sect}},
	} {
		if _, err := admin.Exec(ctx, `INSERT INTO chapter_fact
			(novel_id, chapter_index, prompt_version, ordinal, text, category, subjects, source_hash, requested_model)
			VALUES ($1, $2, 'tagged-facts-v3.txt', $3, $4, $5, $6::uuid[], 'h', 'm')`,
			fixture.novelID, row.chapter, i, row.text, row.category, row.subjects); err != nil {
			t.Fatalf("seed fact: %v", err)
		}
	}

	pages, err := store.ListWikiPages(ctx, fixture.novelID, 2)
	if err != nil {
		t.Fatal(err)
	}
	kinds := map[string]string{}
	for _, page := range pages {
		kinds[page.Title] = page.Kind
	}
	if kinds["Hero"] != "character" || kinds["Cloud Gate"] != "organization" {
		t.Fatalf("pages = %+v; want a character and an organization", pages)
	}
	page, err := store.GetWikiPage(ctx, fixture.novelID, sect, 2)
	if err != nil || page.Kind != "organization" || len(page.Facts) != 2 || page.Kinds[hero] != "character" {
		t.Fatalf("organization page = %+v, %v", page, err)
	}
	events, err := store.ListWikiEvents(ctx, fixture.novelID, 2)
	if err != nil || len(events.Facts) != 1 || events.Facts[0].Text != "The Cloud Gate was founded." ||
		events.Kinds[sect] != "organization" {
		t.Fatalf("events at 2 = %+v, %v; want only the chapter 1 event", events, err)
	}
}
