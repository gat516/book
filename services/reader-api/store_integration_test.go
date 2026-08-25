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
	// GetChapter (object-store reads) isn't exercised by this integration suite, so a
	// nil minio client is fine here — adding MinIO as a dependency of this test harness
	// is out of scope; see handlers_test.go's fakeStore for GetChapter's own coverage.
	store, err := newStore(ctx, Config{
		ReaderDatabaseURL: databaseURL, ProgressDatabaseURL: databaseURL,
	}, nil)
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

func seedIntegrationFixture(t *testing.T, admin *pgxpool.Pool) integrationFixture {
	t.Helper()
	ctx := context.Background()
	fixture := integrationFixture{
		novelID: uuid.NewString(), otherNovelID: uuid.NewString(),
		heroID: uuid.NewString(), allyID: uuid.NewString(), futureID: uuid.NewString(),
	}
	_, err := admin.Exec(ctx,
		`INSERT INTO novel (id, title, source_lang, target_lang, ontology)
		 VALUES ($1, 'Reader Test', 'en', 'en', '{}'),
		        ($2, 'Other Novel', 'en', 'en', '{}')`,
		fixture.novelID, fixture.otherNovelID)
	if err != nil {
		t.Fatal(err)
	}
	for _, chapter := range []struct {
		index  int
		status string
	}{{100, "done"}, {220, "done"}, {300, "ingested"}, {500, "done"}} {
		if _, err := admin.Exec(ctx,
			`INSERT INTO chapter
			 (novel_id, chapter_index, raw_hash, raw_uri, source_meta, status)
			 VALUES ($1, $2, $3, $4, '{}', $5)`,
			fixture.novelID, chapter.index,
			fmt.Sprintf("sha256:reader-%d", chapter.index),
			fmt.Sprintf("reader/%d.txt", chapter.index), chapter.status,
		); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter) VALUES
		 ($1, $4, 'character', 'Hero', 1),
		 ($2, $4, 'character', 'Ally', 1),
		 ($3, $4, 'character', 'Future Identity', 500)`,
		fixture.heroID, fixture.allyID, fixture.futureID, fixture.novelID,
	); err != nil {
		t.Fatal(err)
	}
	otherEntity := uuid.NewString()
	if _, err := admin.Exec(ctx,
		`INSERT INTO entity (id, novel_id, kind, canonical, first_seen_chapter)
		 VALUES ($1, $2, 'character', 'Other Hero', 1)`,
		otherEntity, fixture.otherNovelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO alias (entity_id, surface, lang, first_seen_chapter) VALUES
		 ($1, 'Hero', 'en', 1), ($1, 'Secret Monarch', 'en', 500)`, fixture.heroID,
	); err != nil {
		t.Fatal(err)
	}

	var baseFactID int64
	if err := admin.QueryRow(ctx,
		`INSERT INTO fact
		 (novel_id, entity_id, attribute, value, valid_from_chapter, source_chapter)
		 VALUES ($1, $2, 'role', 'wanderer', 50, 50) RETURNING id`,
		fixture.novelID, fixture.heroID,
	).Scan(&baseFactID); err != nil {
		t.Fatal(err)
	}
	var correctionID int64
	if err := admin.QueryRow(ctx,
		`INSERT INTO fact
		 (novel_id, entity_id, attribute, value, kind, supersedes,
		  valid_from_chapter, source_chapter)
		 VALUES ($1, $2, 'role', 'guardian', 'correction', $3, 50, 150) RETURNING id`,
		fixture.novelID, fixture.heroID, baseFactID,
	).Scan(&correctionID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO fact
		 (novel_id, entity_id, attribute, value, kind, supersedes,
		  valid_from_chapter, source_chapter) VALUES
		 ($1, $2, 'role', '', 'retraction', $3, 50, 400),
		 ($1, $2, 'status', 'alive', 'assertion', NULL, 100, 100),
		 ($1, $2, 'title', 'future king', 'assertion', NULL, 300, 100),
		 ($1, $2, 'rank', 'hidden master', 'assertion', NULL, 10, 500)`,
		fixture.novelID, fixture.heroID, correctionID,
	); err != nil {
		t.Fatal(err)
	}

	if _, err := admin.Exec(ctx,
		`INSERT INTO edge
		 (novel_id, src_id, dst_id, rel_type, valid_from_chapter, valid_to_chapter, source_chapter)
		 VALUES
		 ($1, $2, $3, 'mentor', 100, NULL, 100),
		 ($1, $3, $2, 'ally', 100, NULL, 100),
		 ($1, $2, $2, 'self-reflection', 100, NULL, 100),
		 ($1, $2, $3, 'future-enemy', 10, NULL, 500),
		 ($1, $2, $4, 'secret-family', 100, NULL, 100),
		 ($1, $2, $3, 'former-ally', 100, 200, 100)`,
		fixture.novelID, fixture.heroID, fixture.allyID, fixture.futureID,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO event (novel_id, chapter_index, summary, entity_ids) VALUES
		 ($1, 100, 'A visible event', $2),
		 ($1, 500, 'A future event', $3)`,
		fixture.novelID,
		[]uuid.UUID{uuid.MustParse(fixture.heroID), uuid.MustParse(fixture.futureID)},
		[]uuid.UUID{uuid.MustParse(fixture.heroID)},
	); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx,
		`INSERT INTO chunk (novel_id, chapter_index, text, embedding) VALUES
		 ($1, 100, 'visible', $2), ($1, 500, 'future', $2)`,
		fixture.novelID, zeroVector()); err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() {
		for _, statement := range []string{
			`DELETE FROM reader_progress WHERE novel_id = $1`,
			`DELETE FROM fact WHERE novel_id = $1`,
			`DELETE FROM edge WHERE novel_id = $1`,
			`DELETE FROM event WHERE novel_id = $1`,
			`DELETE FROM chunk WHERE novel_id = $1`,
			`DELETE FROM alias WHERE entity_id IN (SELECT id FROM entity WHERE novel_id = $1)`,
			`DELETE FROM entity WHERE novel_id = $1`,
			`DELETE FROM chapter WHERE novel_id = $1`,
			`DELETE FROM novel WHERE id = $1`,
		} {
			_, _ = admin.Exec(context.Background(), statement, fixture.novelID)
		}
		_, _ = admin.Exec(context.Background(), `DELETE FROM entity WHERE novel_id = $1`, fixture.otherNovelID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM novel WHERE id = $1`, fixture.otherNovelID)
	})
	return fixture
}

func TestSpoilerGateEndToEnd(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()

	progress, err := store.AdvanceProgress(ctx, "reader-a", fixture.novelID, 220)
	if err != nil || progress.CurrentChapter != 220 {
		t.Fatalf("advance progress: %#v %v", progress, err)
	}
	progress, err = store.AdvanceProgress(ctx, "reader-a", fixture.novelID, 100)
	if err != nil || progress.CurrentChapter != 220 {
		t.Fatalf("progress must be monotonic: %#v %v", progress, err)
	}
	if _, err := store.AdvanceProgress(ctx, "reader-a", fixture.novelID, 300); !errors.Is(err, ErrChapterNotReady) {
		t.Fatalf("unfinished chapter error = %v", err)
	}
	if _, err := store.GetProgress(ctx, "reader-b", fixture.novelID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("reader-b progress error = %v", err)
	}
	if _, err := store.AdvanceProgress(ctx, "reader-a", uuid.NewString(), 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing novel progress error = %v", err)
	}

	api := &API{store: store}
	endpointCases := []struct {
		name      string
		target    string
		visible   string
		forbidden []string
	}{
		{
			name: "entity", target: "/novels/" + fixture.novelID + "/entity/" + fixture.heroID + "?at=220",
			visible: "guardian", forbidden: []string{"hidden master", "future king", "Secret Monarch"},
		},
		{
			name: "wiki", target: "/novels/" + fixture.novelID + "/wiki?at=220",
			visible: "Hero", forbidden: []string{"Future Identity", "Other Hero"},
		},
		{
			name: "timeline", target: "/novels/" + fixture.novelID + "/timeline?at=220",
			visible: "A visible event", forbidden: []string{"A future event", "Future Identity"},
		},
		{
			name: "relationships", target: "/novels/" + fixture.novelID + "/relationships/" + fixture.heroID + "?at=220",
			visible: "mentor", forbidden: []string{"future-enemy", "secret-family", "former-ally", "Future Identity"},
		},
	}
	for _, test := range endpointCases {
		t.Run(test.name, func(t *testing.T) {
			response := request(t, api, "GET", test.target, "", "reader-a")
			body := response.Body.String()
			if response.Code != 200 || !strings.Contains(body, test.visible) {
				t.Fatalf("status=%d body=%s", response.Code, body)
			}
			for _, forbidden := range test.forbidden {
				if strings.Contains(body, forbidden) {
					t.Fatalf("response leaked %q: %s", forbidden, body)
				}
			}
		})
	}

	wiki, err := store.ListWiki(ctx, fixture.novelID, 220)
	if err != nil || len(wiki) != 2 || slices.ContainsFunc(wiki, func(e EntitySummary) bool {
		return e.ID == fixture.futureID
	}) {
		t.Fatalf("wiki leaked future entity: %#v %v", wiki, err)
	}
	entity, err := store.GetEntity(ctx, fixture.novelID, fixture.heroID, 220)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(entity.Aliases, []string{"Hero"}) {
		t.Fatalf("aliases leaked: %#v", entity.Aliases)
	}
	facts := map[string]string{}
	for _, fact := range entity.Facts {
		facts[fact.Attribute] = fact.Value
	}
	if facts["role"] != "guardian" || facts["status"] != "alive" {
		t.Fatalf("correction/latest facts wrong: %#v", facts)
	}
	if _, exists := facts["rank"]; exists {
		t.Fatalf("flashback leaked at 220: %#v", facts)
	}
	if _, exists := facts["title"]; exists {
		t.Fatalf("prophecy rendered before story-time: %#v", facts)
	}

	events, err := store.ListTimeline(ctx, fixture.novelID, 220)
	if err != nil || len(events) != 1 || len(events[0].Entities) != 1 || events[0].Entities[0].ID != fixture.heroID {
		t.Fatalf("timeline filtering wrong: %#v %v", events, err)
	}
	relationships, err := store.ListRelationships(ctx, fixture.novelID, fixture.heroID, 220)
	if err != nil || len(relationships) != 3 {
		t.Fatalf("relationships = %#v %v", relationships, err)
	}
	directions := []string{
		relationships[0].Direction, relationships[1].Direction, relationships[2].Direction,
	}
	slices.Sort(directions)
	if !slices.Equal(directions, []string{"incoming", "outgoing", "outgoing"}) {
		t.Fatalf("relationship directions = %#v", directions)
	}
	selfEdges := 0
	for _, relationship := range relationships {
		if relationship.Relation == "self-reflection" {
			selfEdges++
			if relationship.Direction != "outgoing" || relationship.Entity.ID != fixture.heroID {
				t.Fatalf("self edge = %#v", relationship)
			}
		}
	}
	if selfEdges != 1 {
		t.Fatalf("self edge count = %d, want 1", selfEdges)
	}

	entity, err = store.GetEntity(ctx, fixture.novelID, fixture.heroID, 500)
	if err != nil {
		t.Fatal(err)
	}
	facts = map[string]string{}
	for _, fact := range entity.Facts {
		facts[fact.Attribute] = fact.Value
	}
	if facts["rank"] != "hidden master" {
		t.Fatalf("flashback not visible at 500: %#v", facts)
	}
	if _, exists := facts["role"]; exists {
		t.Fatalf("retracted role still visible: %#v", facts)
	}
}

func TestRLSAloneFailsClosedAndDoesNotLeakSettings(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()

	assertMax := func(tx pgx.Tx, query string, want int) {
		t.Helper()
		var maximum int
		if err := tx.QueryRow(ctx, query).Scan(&maximum); err != nil {
			t.Fatal(err)
		}
		if maximum > want {
			t.Fatalf("unsafe query %q returned max %d above %d", query, maximum, want)
		}
	}
	err := store.withReaderTx(ctx, fixture.novelID, 220, func(tx pgx.Tx) error {
		// Deliberately omit every app-layer WHERE. RLS must supply both novel and chapter gates.
		assertMax(tx, `SELECT COALESCE(max(source_chapter), -1) FROM fact`, 220)
		assertMax(tx, `SELECT COALESCE(max(source_chapter), -1) FROM edge`, 220)
		assertMax(tx, `SELECT COALESCE(max(chapter_index), -1) FROM event`, 220)
		assertMax(tx, `SELECT COALESCE(max(first_seen_chapter), -1) FROM entity`, 220)
		assertMax(tx, `SELECT COALESCE(max(first_seen_chapter), -1) FROM alias`, 220)
		assertMax(tx, `SELECT COALESCE(max(chapter_index), -1) FROM chunk`, 220)
		var entityCount int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM entity`).Scan(&entityCount); err != nil {
			return err
		}
		if entityCount != 2 {
			return fmt.Errorf("novel-scoped RLS returned %d entities, want 2", entityCount)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	connection, err := store.readerDB.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Release()
	for _, table := range []string{"fact", "edge", "event", "entity", "alias", "chunk"} {
		var count int
		if err := connection.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 0 {
			t.Fatalf("%s returned %d rows without SET LOCAL", table, count)
		}
	}

	tx, err := connection.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx,
		`SELECT set_config('app.novel_id', $1, true), set_config('app.current_chapter', '500', true)`,
		fixture.novelID); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM fact`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("local gate leaked after commit: %d rows", count)
	}
}

func TestDatabaseRolesAreLeastPrivilege(t *testing.T) {
	store, _ := integrationDatabase(t)
	ctx := context.Background()

	var count int
	if err := store.readerDB.QueryRow(ctx, `SELECT count(*) FROM reader_progress`).Scan(&count); err == nil {
		t.Fatal("rls_reader unexpectedly read reader_progress")
	}
	if err := store.progressDB.QueryRow(ctx, `SELECT count(*) FROM fact`).Scan(&count); err == nil {
		t.Fatal("reader_progress_writer unexpectedly read fact")
	}
}
