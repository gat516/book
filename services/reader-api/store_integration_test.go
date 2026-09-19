package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type integrationFixture struct {
	novelID      string
	otherNovelID string
	heroID       string
	allyID       string
	futureID     string
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

func zeroVector() string {
	values := make([]string, 768)
	for index := range values {
		values[index] = "0"
	}
	return "[" + strings.Join(values, ",") + "]"
}

// The fixture is a records generation, not a legacy graph: one novel with three
// chapters, two published runs (1 and 2), one unpublished run (3), a retired generation
// that must stay invisible, and identity that is deliberately split across chapters so
// "future alias" and "future evidence" have something real to hide.
func seedIntegrationFixture(t *testing.T, admin *pgxpool.Pool) integrationFixture {
	t.Helper()
	ctx := context.Background()
	fixture := integrationFixture{
		novelID: uuid.NewString(), otherNovelID: uuid.NewString(),
		heroID: uuid.NewString(), allyID: uuid.NewString(), futureID: uuid.NewString(),
	}
	for _, novelID := range []string{fixture.novelID, fixture.otherNovelID} {
		if _, err := admin.Exec(ctx,
			`INSERT INTO novel (id, title, source_lang, target_lang, ontology)
			 VALUES ($1, 'Records Fixture', 'zh', 'en', '{"kinds":["character","group"]}'::jsonb)`,
			novelID); err != nil {
			t.Fatalf("seed novel: %v", err)
		}
	}
	for chapter := 1; chapter <= 3; chapter++ {
		if _, err := admin.Exec(ctx,
			// raw_hash is the content-dedup key and is unique per novel, so each chapter
			// needs its own.
			`INSERT INTO chapter (novel_id, chapter_index, raw_uri, raw_hash, source_meta)
			 VALUES ($1, $2, 'raw://c', $3, '{}'::jsonb)`,
			fixture.novelID, chapter, fmt.Sprintf("hash-%d", chapter)); err != nil {
			t.Fatalf("seed chapter: %v", err)
		}
	}
	// The insert trigger already made an active generation; take it, and add a retired
	// one so a query that forgets to scope by generation shows up as a failure.
	var generation string
	if err := admin.QueryRow(ctx,
		`SELECT active_record_generation::text FROM novel WHERE id=$1`, fixture.novelID).Scan(&generation); err != nil {
		t.Fatalf("active generation: %v", err)
	}
	retired := uuid.NewString()
	if _, err := admin.Exec(ctx,
		`INSERT INTO record_generation (id, novel_id, state, ontology, prompt_version,
		   checks_version, extraction_model, source_lang, target_lang, retired_at)
		 VALUES ($1, $2, 'retired', '{"kinds":["character"]}'::jsonb, 'v0',
		   'v0', 'test-model', 'zh', 'en', now())`,
		retired, fixture.novelID); err != nil {
		t.Fatalf("seed retired generation: %v", err)
	}
	entities := []struct {
		id      string
		name    string
		chapter int
	}{{fixture.heroID, "Hero", 1}, {fixture.allyID, "Ally", 2}, {fixture.futureID, "Future Identity", 3}}
	for _, entity := range entities {
		if _, err := admin.Exec(ctx,
			`INSERT INTO entity (id, novel_id, record_generation_id, kind, canonical, first_seen_chapter)
			 VALUES ($1, $2, $3, 'character', $4, $5)`,
			entity.id, fixture.novelID, generation, entity.name, entity.chapter); err != nil {
			t.Fatalf("seed entity: %v", err)
		}
	}
	// The hero gains a second name in chapter 3: a chapter-1 reader must not see it.
	for _, alias := range []struct {
		entity  string
		surface string
		chapter int
	}{{fixture.heroID, "Hero", 1}, {fixture.heroID, "The Sworn Blade", 3}, {fixture.allyID, "Ally", 2}} {
		if _, err := admin.Exec(ctx,
			`INSERT INTO alias (entity_id, surface, lang, first_seen_chapter, record_generation_id)
			 VALUES ($1, $2, 'zh', $3, $4)`,
			alias.entity, alias.surface, alias.chapter, generation); err != nil {
			t.Fatalf("seed alias: %v", err)
		}
	}
	runs := map[int]string{}
	for chapter, status := range map[int]string{1: "published", 2: "published", 3: "processing"} {
		runID := uuid.NewString()
		runs[chapter] = runID
		// Only a published run carries a publication version; the processing run must
		// stay NULL so "unpublished is invisible" is tested against the real shape.
		var publication *int64
		if status == "published" {
			version := int64(chapter)
			publication = &version
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_run (id, novel_id, generation_id, chapter_index, source_hash,
			   request_identity, extraction_model, status, publication_version)
			 VALUES ($1, $2, $3, $4, $6, 'identity', 'test-model', $5, $7)`,
			runID, fixture.novelID, generation, chapter, status, fmt.Sprintf("hash-%d", chapter),
			publication); err != nil {
			t.Fatalf("seed run: %v", err)
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_passage (novel_id, generation_id, run_id, chapter_index, passage_id,
			   text, char_start, char_end, ordinal, source_hash)
			 VALUES ($1, $2, $3, $4, 'p001', 'passage text', 0, 12, 1, $5)`,
			fixture.novelID, generation, runID, chapter, fmt.Sprintf("hash-%d", chapter)); err != nil {
			t.Fatalf("seed passage: %v", err)
		}
		rowID := uuid.NewString()
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_row (id, novel_id, generation_id, run_id, original_index, record_type,
			   source_chapter, source_hash)
			 VALUES ($1, $2, $3, $4, 1, $5, $6, $7)`,
			rowID, fixture.novelID, generation, runID,
			map[int]string{1: "IDENTITY", 2: "EVENT", 3: "EVENT"}[chapter], chapter,
			fmt.Sprintf("hash-%d", chapter)); err != nil {
			t.Fatalf("seed row: %v", err)
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_value (row_id, field_name, source_value) VALUES ($1, 'what', $2)`,
			rowID, fmt.Sprintf("chapter %d happening", chapter)); err != nil {
			t.Fatalf("seed value: %v", err)
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_participant (row_id, ordinal, field_name, surface, entity_id)
			 VALUES ($1, 1, 'who', 'Hero', $2)`, rowID, fixture.heroID); err != nil {
			t.Fatalf("seed participant: %v", err)
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO record_evidence (row_id, run_id, passage_id, quote)
			 VALUES ($1, $2, 'p001', 'quoted words')`, rowID, runID); err != nil {
			t.Fatalf("seed evidence: %v", err)
		}
	}
	t.Cleanup(func() {
		for _, novelID := range []string{fixture.novelID, fixture.otherNovelID} {
			for _, statement := range []string{
				`DELETE FROM reader_progress WHERE novel_id = $1`,
				`DELETE FROM chunk WHERE novel_id = $1`,
				`DELETE FROM chapter WHERE novel_id = $1`,
				`UPDATE novel SET active_record_generation=NULL WHERE id=$1`,
				`DELETE FROM record_generation WHERE novel_id=$1`,
				`DELETE FROM novel WHERE id = $1`,
			} {
				_, _ = admin.Exec(context.Background(), statement, novelID)
			}
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

// A reader at chapter 1 sees chapter 1 and nothing later, on every records surface.
func TestSpoilerGateEndToEnd(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 1)
	ctx := context.Background()

	rows, err := store.ListRecords(ctx, fixture.novelID, 1, 1)
	if err != nil {
		t.Fatalf("chapter 1 rows: %v", err)
	}
	if len(rows.Rows) != 1 || rows.Rows[0].SourceChapter != 1 {
		t.Fatalf("chapter 1 rows = %+v", rows.Rows)
	}
	if len(rows.Rows[0].Evidence) != 1 || rows.Rows[0].Evidence[0].PassageID != "p001" {
		t.Fatalf("evidence not hydrated: %+v", rows.Rows[0].Evidence)
	}
	if _, err := store.ListRecords(ctx, fixture.novelID, 2, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("chapter 2 at=1 must be not found, got %v", err)
	}

	timeline, err := store.ListTimeline(ctx, fixture.novelID, 1)
	if err != nil {
		t.Fatalf("timeline: %v", err)
	}
	for _, row := range timeline.Rows {
		if row.SourceChapter > 1 {
			t.Fatalf("timeline leaked chapter %d", row.SourceChapter)
		}
	}

	wiki, err := store.ListWiki(ctx, fixture.novelID, 1)
	if err != nil {
		t.Fatalf("wiki: %v", err)
	}
	for _, entity := range wiki.Entities {
		if entity.FirstSeenChapter > 1 {
			t.Fatalf("wiki leaked entity first seen in chapter %d", entity.FirstSeenChapter)
		}
	}

	entity, err := store.GetEntity(ctx, fixture.novelID, fixture.heroID, 1)
	if err != nil {
		t.Fatalf("entity: %v", err)
	}
	if slices.Contains(entity.Entity.Aliases, "The Sworn Blade") {
		t.Fatal("a chapter-3 alias reached a chapter-1 reader")
	}
	if _, err := store.GetEntity(ctx, fixture.novelID, fixture.futureID, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("future entity must be not found, got %v", err)
	}
}

func TestRecordReviewRejectHidesRowsAndProjectionCanRestoreThem(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 3)
	ctx := context.Background()
	var generation, rowID string
	if err := admin.QueryRow(ctx, `SELECT active_record_generation::text FROM novel WHERE id=$1`, fixture.novelID).Scan(&generation); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRow(ctx, `SELECT id::text FROM record_row WHERE novel_id=$1 AND generation_id=$2 AND source_chapter=1`, fixture.novelID, generation).Scan(&rowID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO record_review_decision
		(novel_id,generation_id,row_id,source_chapter,decision,actor,reason,request_id,request_fingerprint)
		VALUES ($1,$2,$3,1,'rejected','reviewer','bad extraction','review-reject','fixture')`, fixture.novelID, generation, rowID); err != nil {
		t.Fatalf("reject row: %v", err)
	}
	rows, err := store.ListRecords(ctx, fixture.novelID, 1, 3)
	if err != nil {
		t.Fatalf("ordinary records: %v", err)
	}
	if len(rows.Rows) != 0 {
		t.Fatalf("rejected row leaked through ordinary records: %+v", rows.Rows)
	}
	review, err := store.ListRecordReviews(ctx, fixture.novelID, 1, 3)
	if err != nil {
		t.Fatalf("review projection: %v", err)
	}
	if len(review.Items) != 1 || review.Items[0].Decision == nil || review.Items[0].Decision.Decision != "rejected" {
		t.Fatalf("rejected row missing from review projection: %+v", review.Items)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO record_review_decision
		(novel_id,generation_id,row_id,source_chapter,decision,actor,reason,request_id,request_fingerprint)
		VALUES ($1,$2,$3,1,'accepted','reviewer','restored','review-accept','fixture-2')`, fixture.novelID, generation, rowID); err != nil {
		t.Fatalf("accept row: %v", err)
	}
	rows, err = store.ListRecords(ctx, fixture.novelID, 1, 3)
	if err != nil || len(rows.Rows) != 1 {
		t.Fatalf("accepted row not restored: rows=%d err=%v", len(rows.Rows), err)
	}
}

// DISPLAY_SCAN's mention_span is only a presentation coordinate. Identity must come
// from the active generation's published who's-who binding; the old graph binding path
// is gone. This also proves the binding cannot widen the chapter gate by itself.
func TestMentionSpanUsesPublishedRecordBindingAndChapterGate(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 1)
	ctx := context.Background()
	var generation, runID string
	if err := admin.QueryRow(ctx,
		`SELECT n.active_record_generation::text,r.id::text
		   FROM novel n JOIN record_run r ON r.novel_id=n.id AND r.generation_id=n.active_record_generation
		  WHERE n.id=$1 AND r.chapter_index=1`, fixture.novelID,
	).Scan(&generation, &runID); err != nil {
		t.Fatalf("published generation/run: %v", err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO mention_span (novel_id,chapter_index,entity_id,char_start,char_end)
		 VALUES ($1,1,NULL,4,8),($1,2,NULL,4,8)`, fixture.novelID); err != nil {
		t.Fatalf("mention spans: %v", err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO record_mention_binding
		   (novel_id,generation_id,run_id,entity_id,source_chapter,char_start,char_end)
		 VALUES ($1,$2,$3,$4,1,4,8)`, fixture.novelID, generation, runID, fixture.heroID); err != nil {
		t.Fatalf("mention binding: %v", err)
	}
	var got []SpanView
	if err := store.withReaderTx(ctx, fixture.novelID, 1, func(tx pgx.Tx) error {
		var err error
		got, err = chapterSpansInTx(ctx, tx, fixture.novelID, 1, generation)
		return err
	}); err != nil {
		t.Fatalf("chapter spans: %v", err)
	}
	if len(got) != 1 || got[0].EntityID == nil || *got[0].EntityID != fixture.heroID {
		t.Fatalf("bound span = %+v", got)
	}
	var future []SpanView
	if err := store.withReaderTx(ctx, fixture.novelID, 1, func(tx pgx.Tx) error {
		var err error
		future, err = chapterSpansInTx(ctx, tx, fixture.novelID, 2, generation)
		return err
	}); err != nil {
		t.Fatalf("future chapter spans: %v", err)
	}
	if len(future) != 0 {
		t.Fatalf("future span leaked through RLS: %+v", future)
	}
}

// The glossary endpoint must survive the legacy graph retirement. In particular it
// must not depend on glossary_binding, which 0089 drops, and an entity id is only
// attached once that generation-local identity is visible at the reader's chapter.
func TestGlossaryUsesRecordsIdentityWithoutLegacyBinding(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 1)
	ctx := context.Background()
	for _, term := range []struct {
		source, target, entity string
		chapter                int
	}{
		{"hero-term", "Hero Term", fixture.heroID, 1},
		{"future-term", "Future Term", fixture.futureID, 1},
		{"unseen-term", "Unseen Term", "", 3},
	} {
		var entity any
		if term.entity != "" {
			entity = term.entity
		}
		if _, err := admin.Exec(ctx,
			`INSERT INTO glossary (novel_id,source_term,target_term,entity_id,version,locked_at_chapter)
			 VALUES ($1,$2,$3,$4,1,$5)`, fixture.novelID, term.source, term.target, entity, term.chapter); err != nil {
			t.Fatalf("glossary %s: %v", term.source, err)
		}
	}
	terms, err := store.ListGlossary(ctx, fixture.novelID, 1)
	if err != nil {
		t.Fatalf("list glossary after legacy retirement: %v", err)
	}
	if len(terms) != 2 {
		t.Fatalf("visible terms = %+v, want two chapter-1 locks", terms)
	}
	for _, term := range terms {
		if term.SourceTerm == "hero-term" && (term.EntityID == nil || *term.EntityID != fixture.heroID) {
			t.Fatalf("hero entity was not attached: %+v", term)
		}
		if term.SourceTerm == "future-term" && term.EntityID != nil {
			t.Fatalf("future entity leaked through glossary: %+v", term)
		}
	}
}

// An unpublished run's rows never reach a reader, even at a chapter they may read.
func TestUnpublishedRunIsInvisible(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 3)

	rows, err := store.ListRecords(context.Background(), fixture.novelID, 3, 3)
	if err != nil {
		t.Fatalf("chapter 3 rows: %v", err)
	}
	if len(rows.Rows) != 0 {
		t.Fatalf("unpublished run exposed %d rows", len(rows.Rows))
	}
	// A chapter without facts is not done (0110); a RECORDS run in progress no longer
	// means anything to the status.
	if rows.Status.ExtractionStatus != "pending" {
		t.Fatalf("status = %q, want pending", rows.Status.ExtractionStatus)
	}
}

func markFactsDone(t *testing.T, admin *pgxpool.Pool, novelID string, chapter int) {
	t.Helper()
	if _, err := admin.Exec(context.Background(),
		`UPDATE chapter SET translation_ready=true, facts_count=0 WHERE novel_id=$1 AND chapter_index=$2`,
		novelID, chapter); err != nil {
		t.Fatalf("mark chapter %d facts done: %v", chapter, err)
	}
}

// A published extraction with no accepted rows has completed all the rendering work it
// can possibly have. An empty record_rendering table must not leave the UI polling it
// forever as if an English-rendering job were still running.
func TestPublishedEmptyRunHasReadyRenderingStatus(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 1)
	if _, err := admin.Exec(context.Background(),
		`DELETE FROM record_row WHERE novel_id=$1 AND source_chapter=1`, fixture.novelID); err != nil {
		t.Fatalf("remove chapter 1 records: %v", err)
	}
	markFactsDone(t, admin, fixture.novelID, 1)

	rows, err := store.ListRecords(context.Background(), fixture.novelID, 1, 1)
	if err != nil {
		t.Fatalf("chapter 1 rows: %v", err)
	}
	if len(rows.Rows) != 0 || rows.Status.ExtractionStatus != "ready" || rows.Status.RenderingStatus != "ready" {
		t.Fatalf("empty published run status = %+v rows=%d", rows.Status, len(rows.Rows))
	}
}

func TestPublishedRowsWithoutRenderingsStayPending(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 1)
	markFactsDone(t, admin, fixture.novelID, 1)

	rows, err := store.ListRecords(context.Background(), fixture.novelID, 1, 1)
	if err != nil {
		t.Fatalf("chapter 1 rows: %v", err)
	}
	if len(rows.Rows) == 0 || rows.Status.ExtractionStatus != "ready" || rows.Status.RenderingStatus != "pending" {
		t.Fatalf("unrendered published row status = %+v rows=%d", rows.Status, len(rows.Rows))
	}
}

// RLS is the second lock: with no GUCs set, the reader role sees nothing at all, and a
// query for another novel returns nothing even inside a valid reader transaction.
func TestRLSAloneFailsClosedAndDoesNotLeakSettings(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	setProgress(t, admin, fixture.novelID, 3)
	ctx := context.Background()

	conn, err := store.readerDB.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer conn.Release()
	for _, table := range []string{"record_row", "record_value", "record_participant",
		"record_evidence", "record_passage", "record_reference", "entity", "alias"} {
		var count int
		if err := conn.QueryRow(ctx, fmt.Sprintf("SELECT count(*) FROM %s", table)).Scan(&count); err != nil {
			t.Fatalf("count %s without GUCs: %v", table, err)
		}
		if count != 0 {
			t.Fatalf("%s returned %d rows with no reader GUCs set", table, count)
		}
	}

	// Right transaction, wrong novel: the other novel's id must not widen the gate.
	other, err := store.ListRecords(ctx, fixture.otherNovelID, 1, 3)
	if err != nil && !errors.Is(err, ErrNotFound) {
		t.Fatalf("other novel: %v", err)
	}
	if len(other.Rows) != 0 {
		t.Fatalf("other novel exposed %d rows", len(other.Rows))
	}
}

func TestDatabaseRolesAreLeastPrivilege(t *testing.T) {
	_, admin := integrationDatabase(t)
	ctx := context.Background()
	for _, table := range []string{"record_row", "record_value", "record_participant",
		"record_evidence", "record_passage", "record_rendering", "record_reference",
		"record_run", "record_generation", "record_drop"} {
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

func TestRecordsStatusReportsSafeFailureWithinReaderGate(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	for _, chapter := range []int{1, 3} {
		if _, err := admin.Exec(ctx, `UPDATE chapter SET enrichment_attempts=1,
   enrichment_retry_at=now()+interval '5 minutes' WHERE novel_id=$1 AND chapter_index=$2`, fixture.novelID, chapter); err != nil {
			t.Fatal(err)
		}
		code := "provider_invalid_json"
		if chapter == 3 {
			code = "credential_rejected"
		}
		if _, err := admin.Exec(ctx, `INSERT INTO chapter_failure (novel_id,chapter_index,stage,error_type,error_code)
   VALUES ($1,$2,'character_names','ProviderResponseError',$3)`, fixture.novelID, chapter, code); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), `DELETE FROM chapter_failure WHERE novel_id=$1`, fixture.novelID)
	})
	// A newer failure beyond the reader's cap must not replace the visible cause.
	if err := store.withReaderTx(ctx, fixture.novelID, 1, func(tx pgx.Tx) error {
		_, status, err := recordsStatusFor(ctx, tx, fixture.novelID, nil, 1)
		if err != nil {
			return err
		}
		if status.RetryCategory == nil || *status.RetryCategory != "provider_invalid_json" || status.RetryAt == nil {
			t.Fatalf("wrong retry status: %+v", status)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
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
	if err := admin.QueryRow(ctx, `INSERT INTO character (novel_id, source_term, first_seen_chapter)
		VALUES ($1, 'hero-source', 1) RETURNING id::text`, fixture.novelID).Scan(&hero); err != nil {
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
