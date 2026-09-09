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
		RepairOperatorDatabaseURL: databaseURL,
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
			`UPDATE novel SET active_graph_revision=NULL WHERE id=$1`,
			`DELETE FROM graph_revision WHERE novel_id=$1`,
			`DELETE FROM novel WHERE id = $1`,
		} {
			_, _ = admin.Exec(context.Background(), statement, fixture.novelID)
		}
		_, _ = admin.Exec(context.Background(), `DELETE FROM entity WHERE novel_id = $1`, fixture.otherNovelID)
		_, _ = admin.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, fixture.otherNovelID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, fixture.otherNovelID)
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
			visible: `"events":[]`, forbidden: []string{"A visible event", "A future event", "Future Identity"},
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
	if err != nil || len(events) != 0 {
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

func TestChapterKnowledgeActivityIsIncrementalAndSpoilerGated(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := store.AdvanceProgress(ctx, "chapter-knowledge-reader", fixture.novelID, 220); err != nil {
		t.Fatal(err)
	}
	var revision string
	var generation, version int64
	if err := admin.QueryRow(ctx, `SELECT active_graph_revision::text,generation,version FROM novel n JOIN graph_revision r ON r.id=n.active_graph_revision WHERE n.id=$1`, fixture.novelID).Scan(&revision, &generation, &version); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO term_rendering_occurrence(novel_id,chapter_index,char_start,char_end,source_term,display_term,method)
		VALUES($1,100,0,4,'Hero','Hero','aligned'),($1,220,0,4,'Term','Term','aligned'),
		       ($1,500,0,6,'Secret','Secret','aligned')`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	translatedTerms, err := store.ChapterKnowledge(ctx, fixture.novelID, 220, 220)
	if err != nil {
		t.Fatal(err)
	}
	if !translatedTerms.TermsExtracted || translatedTerms.FactsExtracted {
		t.Fatalf("translation term occurrence: terms_extracted=%v facts_extracted=%v",
			translatedTerms.TermsExtracted, translatedTerms.FactsExtracted)
	}
	var visibleRun, futureRun string
	for _, row := range []struct {
		chapter int
		target  *string
	}{{100, &visibleRun}, {500, &futureRun}} {
		if err := admin.QueryRow(ctx, `INSERT INTO chapter_knowledge_run(novel_id,chapter_index,revision_id,mode,state,input_hash,display_hash,model_identity,graph_generation,graph_version)
			VALUES($1,$2,$3,'ordinary','published','source','display','model',$4,$5) RETURNING id::text`, fixture.novelID, row.chapter, revision, generation, version).Scan(row.target); err != nil {
			t.Fatal(err)
		}
		if _, err := admin.Exec(ctx, `INSERT INTO chapter_knowledge_activity(run_id,novel_id,chapter_index,item_kind,item_key,phase,payload,idempotency_key)
			VALUES($1,$2,$3,'fact','fact:1','proposed','{}','proposal'),($1,$2,$3,'fact','fact:1','published','{}','publication')`, *row.target, fixture.novelID, row.chapter); err != nil {
			t.Fatal(err)
		}
	}
	view, err := store.ChapterKnowledge(ctx, fixture.novelID, 100, 220)
	if err != nil {
		t.Fatal(err)
	}
	if len(view.Terms) != 1 || view.Terms[0].SourceTerm != "Hero" {
		t.Fatalf("terms=%+v", view.Terms)
	}
	if !view.TermsExtracted || !view.FactsExtracted {
		t.Fatalf("published all-scope run: terms_extracted=%v facts_extracted=%v",
			view.TermsExtracted, view.FactsExtracted)
	}
	first, err := store.ChapterKnowledgeActivity(ctx, fixture.novelID, 100, 220, visibleRun, 0)
	if err != nil || len(first) != 2 {
		t.Fatalf("activity=%+v err=%v", first, err)
	}
	second, err := store.ChapterKnowledgeActivity(ctx, fixture.novelID, 100, 220, visibleRun, first[0].Sequence)
	if err != nil || len(second) != 1 || second[0].Phase != "published" {
		t.Fatalf("incremental=%+v err=%v", second, err)
	}
	leaked, err := store.ChapterKnowledgeActivity(ctx, fixture.novelID, 500, 220, futureRun, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(leaked) != 0 {
		t.Fatalf("future activity leaked: %+v", leaked)
	}
}

func TestChapterKnowledgeReportsStagingGraphExtractionWithoutExposingClaims(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := store.AdvanceProgress(ctx, "staging-extraction-reader", fixture.novelID, 220); err != nil {
		t.Fatal(err)
	}
	var stagingRevision string
	if err := admin.QueryRow(ctx, `INSERT INTO graph_revision(novel_id,state,trusted,legacy,ontology,snapshot)
		VALUES($1,'staging',false,false,'{}','{}') RETURNING id::text`, fixture.novelID).Scan(&stagingRevision); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), `DELETE FROM graph_job WHERE revision_id=$1`, stagingRevision)
	})
	if _, err := admin.Exec(ctx, `INSERT INTO graph_job(revision_id,chapter_index,state,input_hash,model_identity,generation,output)
		VALUES($1,220,'done','input','model',1,
		'{"diagnostics":{"verified_identities":16,"verified_claims":10,"published_facts":7}}')`, stagingRevision); err != nil {
		t.Fatal(err)
	}

	view, err := store.ChapterKnowledge(ctx, fixture.novelID, 220, 220)
	if err != nil {
		t.Fatal(err)
	}
	if view.FactsExtracted {
		t.Fatal("staging extraction must not mark active-revision facts as published")
	}
	if len(view.Facts) != 0 {
		t.Fatalf("untrusted staging claims leaked into cards: %+v", view.Facts)
	}
	if view.GraphExtraction == nil || view.GraphExtraction.State != "done" ||
		view.GraphExtraction.VerifiedTerms != 16 || view.GraphExtraction.VerifiedClaims != 10 ||
		view.GraphExtraction.PublishedFactRows != 7 {
		t.Fatalf("graph extraction summary=%+v", view.GraphExtraction)
	}

	// Even aggregate staging results obey the caller's chapter ceiling.
	hidden, err := store.ChapterKnowledge(ctx, fixture.novelID, 220, 100)
	if err != nil {
		t.Fatal(err)
	}
	if hidden.GraphExtraction != nil {
		t.Fatalf("future staging summary leaked: %+v", hidden.GraphExtraction)
	}
}

func TestChapterKnowledgeShowsRetractedOriginalButNotRetractionRow(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()
	if _, err := store.AdvanceProgress(ctx, "retraction-reader", fixture.novelID, 220); err != nil {
		t.Fatal(err)
	}
	var factID int64
	var revision string
	if err := admin.QueryRow(ctx, `SELECT f.id,f.revision_id::text FROM fact f
		WHERE f.novel_id=$1 AND f.source_chapter<=220 AND f.kind<>'retraction' ORDER BY f.id LIMIT 1`, fixture.novelID).
		Scan(&factID, &revision); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO fact
		(novel_id,entity_id,attribute,value,value_en,valid_from_chapter,source_chapter,
		 confidence,kind,supersedes,revision_id,evidence_id,claim_key)
		SELECT novel_id,entity_id,attribute,value,NULL,valid_from_chapter,source_chapter,
		 confidence,'retraction',id,revision_id,evidence_id,'integration-retraction'
		FROM fact WHERE id=$1`, factID); err != nil {
		t.Fatal(err)
	}
	var chapter int
	if err := admin.QueryRow(ctx, "SELECT source_chapter FROM fact WHERE id=$1", factID).Scan(&chapter); err != nil {
		t.Fatal(err)
	}
	view, err := store.ChapterKnowledge(ctx, fixture.novelID, chapter, 220)
	if err != nil {
		t.Fatal(err)
	}
	foundOriginal := false
	for _, fact := range view.Facts {
		if fact.Kind == "retraction" {
			t.Fatalf("retraction successor rendered as a fact: %+v", fact)
		}
		if fact.ID == factID {
			foundOriginal = true
			if fact.Status != "retracted" {
				t.Fatalf("original status=%q, want retracted", fact.Status)
			}
		}
	}
	if !foundOriginal {
		t.Fatal("retracted original should remain visible as history")
	}
	_ = revision // kept with the fixture query to prove both rows target the active revision
}

func TestEntityRenderingsExposeOnlyBoundChapterSafeChoices(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()

	var revisionID string
	if err := admin.QueryRow(ctx, `SELECT revision_id::text FROM entity WHERE id=$1`, fixture.heroID).Scan(&revisionID); err != nil {
		t.Fatal(err)
	}
	evidenceID := uuid.NewString()
	mentionID := uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO graph_evidence
		(id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote)
		VALUES ($1,$2,$3,100,'sha256:rendering',0,2,'林峰')`, evidenceID, revisionID, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO source_mention
		(id,revision_id,novel_id,chapter_index,surface,kind,evidence_id)
		VALUES ($1,$2,$3,100,'林峰','character',$4)`, mentionID, revisionID, fixture.novelID, evidenceID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO mention_binding
		(revision_id,mention_id,known_from_chapter,entity_id,evidence_id)
		VALUES ($1,$2,100,$3,$4)`, revisionID, mentionID, fixture.heroID, evidenceID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO character_name_review
		(novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,
		 candidates,reason,status,term_role,rendering_method,selected_target,selection_source,reviewed_at)
		VALUES
		($1,'林峰',100,'sha256:rendering',0,2,'林峰',
		 '[{"target_term":"Lin Feng","pronunciation":["lín","fēng"],"segmentation":"林|峰","method":"pinyin"},{"target_term":"Lin-feng","pronunciation":[],"segmentation":"","method":"pinyin"}]',
		 'multiple_pronunciations','pending','chinese_person','pinyin',NULL,NULL,NULL),
		($1,'旧名',100,'sha256:locked',0,2,'旧名',
		 '[{"target_term":"Old Name","pronunciation":[],"segmentation":"","method":"semantic_translation"},{"target_term":"Former Name","pronunciation":[],"segmentation":"","method":"semantic_translation"}]',
		 'semantic_translation','approved','semantic_term','semantic_translation','Old Name','offered',now()),
		($1,'未来名',500,'sha256:future',0,3,'未来名',
		 '[{"target_term":"Future Name","pronunciation":[],"segmentation":"","method":"semantic_translation"}]',
		 'semantic_translation','approved','semantic_term','semantic_translation','Future Name','offered',now())`, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO glossary
		(novel_id,source_term,target_term,entity_id,version,locked_at_chapter,constraint_class)
		VALUES ($1,'旧名','Old Name',$2,1,100,'semantic_term'),
		       ($1,'未来名','Future Name',$2,2,500,'semantic_term')`, fixture.novelID, fixture.heroID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), `DELETE FROM mention_binding WHERE revision_id=$1 AND mention_id=$2`, revisionID, mentionID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM source_mention WHERE revision_id=$1 AND id=$2`, revisionID, mentionID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM graph_evidence WHERE id=$1`, evidenceID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM glossary WHERE novel_id=$1`, fixture.novelID)
		_, _ = admin.Exec(context.Background(), `DELETE FROM character_name_review WHERE novel_id=$1`, fixture.novelID)
	})

	entity, err := store.GetEntity(ctx, fixture.novelID, fixture.heroID, 220)
	if err != nil {
		t.Fatal(err)
	}
	if len(entity.Renderings) != 2 {
		t.Fatalf("renderings = %#v, want one pending and one locked", entity.Renderings)
	}
	bySource := make(map[string]TermRenderingView, len(entity.Renderings))
	for _, rendering := range entity.Renderings {
		bySource[rendering.SourceTerm] = rendering
	}
	if pending := bySource["林峰"]; pending.Status != "pending" || pending.TargetTerm != nil || len(pending.Candidates) != 2 {
		t.Fatalf("pending rendering = %#v", pending)
	}
	if locked := bySource["旧名"]; locked.Status != "locked" || locked.TargetTerm == nil || *locked.TargetTerm != "Old Name" || len(locked.Candidates) != 2 {
		t.Fatalf("locked rendering = %#v", locked)
	}
	if _, leaked := bySource["未来名"]; leaked {
		t.Fatalf("future rendering leaked at chapter 220: %#v", entity.Renderings)
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

func TestChapterMentionSelectionFollowsActiveRevision(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID, legacyEntityID, managedEntityID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	managedRevisionID, mentionID, evidenceID, displayID := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology)
		VALUES ($1,'Mention authorization test','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status)
		VALUES ($1,1,'sha256:mention','mention/raw','{}','done')`, novelID); err != nil {
		t.Fatal(err)
	}
	var legacyRevisionID string
	if err := admin.QueryRow(ctx, `SELECT active_graph_revision::text FROM novel WHERE id=$1`, novelID).Scan(&legacyRevisionID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter)
		VALUES ($1,$3,'character','Legacy Hero',1),($2,$3,'character','Managed Hero',1)`,
		legacyEntityID, managedEntityID, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO mention_span(novel_id,chapter_index,entity_id,char_start,char_end)
		VALUES ($1,1,$2,0,6)`, novelID, legacyEntityID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO graph_revision
		(id,novel_id,state,trusted,legacy,ontology,model,snapshot)
		VALUES ($1,$2,'staging',false,false,'{}','{}',
		'{"chapters":[{"chapter":1,"source_hash":"sha256:mention"}]}')`,
		managedRevisionID, novelID); err != nil {
		t.Fatal(err)
	}
	// Managed records need the same revision/generation fence as the pipeline writer. They
	// are inserted before cutover so each reader state exercises the actual RLS query.
	tx, err := admin.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `SELECT set_config('app.graph_revision',$1,true),
		set_config('app.graph_generation','1',true)`, managedRevisionID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `UPDATE entity SET revision_id=$1 WHERE id=$2`, managedRevisionID, managedEntityID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO graph_evidence
		(id,revision_id,novel_id,chapter_index,source_hash,char_start,char_end,quote)
		VALUES ($1,$2,$3,1,'sha256:mention',0,7,'Managed')`, evidenceID, managedRevisionID, novelID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO source_mention
		(id,revision_id,novel_id,chapter_index,surface,kind,evidence_id)
		VALUES ($1,$2,$3,1,'管理者','character',$4)`, mentionID, managedRevisionID, novelID, evidenceID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO mention_binding
		(revision_id,mention_id,known_from_chapter,entity_id,evidence_id)
		VALUES ($1,$2,1,$3,$4)`, managedRevisionID, mentionID, managedEntityID, evidenceID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO display_mention
		(id,revision_id,novel_id,chapter_index,mention_id,char_start,char_end,phrase,display_hash,evidence_id)
		VALUES ($1,$2,$3,1,$4,0,7,'Managed','display-hash',$5)`,
		displayID, managedRevisionID, novelID, mentionID, evidenceID); err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, statement := range []string{
			`DELETE FROM display_mention WHERE revision_id=$1`,
			`DELETE FROM mention_binding WHERE revision_id=$1`,
			`DELETE FROM source_mention WHERE revision_id=$1`,
			`DELETE FROM graph_evidence WHERE revision_id=$1`,
			`DELETE FROM mention_span WHERE novel_id=$1`,
			`DELETE FROM entity WHERE novel_id=$1`,
			`DELETE FROM chapter WHERE novel_id=$1`,
			`UPDATE novel SET active_graph_revision=NULL WHERE id=$1`,
			`DELETE FROM graph_revision WHERE novel_id=$1`,
			`DELETE FROM novel WHERE id=$1`,
		} {
			arg := novelID
			if statement == `DELETE FROM graph_evidence WHERE revision_id=$1` {
				arg = managedRevisionID
			}
			_, _ = admin.Exec(context.Background(), statement, arg)
		}
	})

	read := func() []SpanView {
		t.Helper()
		var spans []SpanView
		if err := store.withReaderTx(ctx, novelID, 1, func(tx pgx.Tx) error {
			var err error
			spans, err = chapterSpansInTx(ctx, tx, novelID, 1)
			return err
		}); err != nil {
			t.Fatal(err)
		}
		return spans
	}
	assertEntity := func(want string) {
		t.Helper()
		spans := read()
		if len(spans) != 1 || spans[0].EntityID == nil || *spans[0].EntityID != want {
			t.Fatalf(`spans=%+v, want one link to %s`, spans, want)
		}
	}
	assertPlaceholder := func() {
		t.Helper()
		spans := read()
		if len(spans) != 1 || spans[0].EntityID != nil {
			t.Fatalf(`spans=%+v, want one placeholder`, spans)
		}
	}

	// No active revision: the legacy presentation row survives, but its entity join is
	// denied by revision RLS.
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID); err != nil {
		t.Fatal(err)
	}
	assertPlaceholder()
	// A staging revision is not reader-authorized.
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=$1 WHERE id=$2`, managedRevisionID, novelID); err != nil {
		t.Fatal(err)
	}
	assertPlaceholder()
	// The trusted legacy graph may expose its verified legacy entity link.
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=$1 WHERE id=$2`, legacyRevisionID, novelID); err != nil {
		t.Fatal(err)
	}
	assertEntity(legacyEntityID)
	// Quarantine removes the entity authorization while retaining the presentation row.
	if _, err := admin.Exec(ctx, `UPDATE graph_revision SET trusted=false WHERE id=$1`, legacyRevisionID); err != nil {
		t.Fatal(err)
	}
	assertPlaceholder()
	// Cutover selects managed display_mention/mention_binding and suppresses the legacy
	// fallback, so the managed entity is the only visible link.
	if _, err := admin.Exec(ctx, `UPDATE graph_revision SET state='archived' WHERE id=$1`, legacyRevisionID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `UPDATE graph_revision SET state='active',trusted=true WHERE id=$1`, managedRevisionID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=$1 WHERE id=$2`, managedRevisionID, novelID); err != nil {
		t.Fatal(err)
	}
	assertEntity(managedEntityID)
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

func TestListVocabularyUsesReaderDefinerAndChapterRedaction(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID, otherID := uuid.NewString(), uuid.NewString()
	for _, id := range []string{novelID, otherID} {
		if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES($1,'Vocabulary reader','en','en','{"kinds":["character"]}')`, id); err != nil {
			t.Fatal(err)
		}
		if _, err := admin.Exec(ctx, `INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) VALUES($1,5,$2,'raw','{}','done'),($1,10,$3,'raw10','{}','done')`, id, "sha256:"+id, "sha256:10"+id); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := admin.Exec(ctx, `INSERT INTO reader_progress(reader_id,novel_id,current_chapter) VALUES('reader-vocab',$1,5)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,gloss,first_seen_chapter,last_seen_chapter,admitted_at_chapter) VALUES($1,'attribute','visible_term',ARRAY['character'],'admitted','old gloss',0,0,0),($1,'attribute','future_term',ARRAY['character'],'admitted','future',10,10,10)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary_alias(novel_id,term_type,surface,name,known_from_chapter,created_by) VALUES($1,'attribute','visible_alias','visible_term',0,'test'),($1,'attribute','future_alias','visible_term',10,'test')`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary_changelog(novel_id,seq,term_type,action,name,changed_at_chapter,old_value,new_value,row_hash,created_by) VALUES($1,1,'attribute','gloss','visible_term',10,'{"gloss":"old gloss"}','{"gloss":"new gloss"}','hash','test')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, q := range []string{`DELETE FROM novel_vocabulary_changelog WHERE novel_id=$1`, `DELETE FROM novel_vocabulary_alias WHERE novel_id=$1`, `DELETE FROM novel_vocabulary WHERE novel_id=$1`, `DELETE FROM reader_progress WHERE novel_id=$1`, `DELETE FROM chapter WHERE novel_id=$1`, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, `DELETE FROM graph_revision WHERE novel_id=$1`, `DELETE FROM novel WHERE id=$1`, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, `DELETE FROM graph_revision WHERE novel_id=$1`, `DELETE FROM novel WHERE id=$1`} {
			_, _ = admin.Exec(context.Background(), q, novelID)
			if strings.Contains(q, "$1") && strings.Contains(q, "novel_id") {
				_, _ = admin.Exec(context.Background(), q, otherID)
			}
		}
	})
	terms, err := store.ListVocabulary(ctx, novelID, 5)
	if err != nil {
		t.Fatal(err)
	}
	var visible *VocabularyTermView
	for i := range terms {
		if terms[i].Name == "visible_term" {
			visible = &terms[i]
		}
		if terms[i].Name == "future_term" {
			t.Fatalf("future term visible at 5: %+v", terms[i])
		}
	}
	if visible == nil || visible.Gloss != "old gloss" || len(visible.Aliases) != 1 || visible.Aliases[0] != "visible_alias" {
		t.Fatalf("visible term at 5=%+v", visible)
	}
	terms, err = store.ListVocabulary(ctx, novelID, 10)
	if err != nil {
		t.Fatal(err)
	}
	visible = nil
	var future *VocabularyTermView
	for i := range terms {
		if terms[i].Name == "visible_term" {
			visible = &terms[i]
		}
		if terms[i].Name == "future_term" {
			future = &terms[i]
		}
	}
	if visible == nil || future == nil {
		t.Fatalf("terms at 10=%+v", terms)
	}
	aliasSet := map[string]bool{}
	for _, alias := range visible.Aliases {
		aliasSet[alias] = true
	}
	if visible.Gloss != "new gloss" || len(visible.Aliases) != 2 || !aliasSet["visible_alias"] || !aliasSet["future_alias"] {
		t.Fatalf("terms at 10=%+v", terms)
	}
	// The definer validates requested_novel against the transaction's reader novel.
	var count int
	if err := store.readerDB.QueryRow(ctx, `SELECT count(*) FROM reader_vocabulary($1,5)`, otherID).Scan(&count); err == nil && count != 0 {
		t.Fatalf("cross-novel function returned %d rows", count)
	}
}

func TestVocabularyGatesEntityNewFactsAndRelationships(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES($1,'Vocabulary paths','en','en','{"kinds":["character"]}')`, novelID); err != nil {
		t.Fatal(err)
	}
	var revision string
	if err := admin.QueryRow(ctx, `SELECT active_graph_revision::text FROM novel WHERE id=$1`, novelID).Scan(&revision); err != nil {
		t.Fatal(err)
	}
	a, b := uuid.NewString(), uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO entity(id,novel_id,kind,canonical,first_seen_chapter) VALUES($1,$3,'character','A',0),($2,$3,'character','B',0)`, a, b, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO fact(novel_id,entity_id,attribute,value,valid_from_chapter,source_chapter) VALUES($1,$2,'description','kept',0,1),($1,$2,'candidate_term','hidden',0,1),($1,$2,'old_description','renamed',0,1)`, novelID, a); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO edge(novel_id,src_id,dst_id,rel_type,valid_from_chapter,source_chapter) VALUES($1,$2,$3,'ally',0,1)`, novelID, a, b); err != nil {
		t.Fatal(err)
	}
	var predecessorFact, predecessorEdge int64
	if err := admin.QueryRow(ctx, `SELECT id FROM fact WHERE novel_id=$1 AND attribute='description'`, novelID).Scan(&predecessorFact); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRow(ctx, `SELECT id FROM edge WHERE novel_id=$1 AND rel_type='ally'`, novelID).Scan(&predecessorEdge); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,first_seen_chapter,last_seen_chapter,admitted_at_chapter) VALUES($1,'attribute','candidate_term',ARRAY['character'],'candidate',0,0,NULL),($1,'attribute','old_description',ARRAY['character'],'admitted',0,0,0)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary(novel_id,term_type,name,kinds,status,first_seen_chapter,last_seen_chapter) VALUES($1,'relation','candidate_relation',ARRAY['character'],'candidate',0,0)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO novel_vocabulary_alias(novel_id,term_type,surface,name,known_from_chapter,created_by) VALUES($1,'attribute','old_description','description',2,'test')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, q := range []string{`DELETE FROM novel_vocabulary_alias WHERE novel_id=$1`, `DELETE FROM novel_vocabulary WHERE novel_id=$1`, `DELETE FROM edge WHERE novel_id=$1`, `DELETE FROM fact WHERE novel_id=$1`, `DELETE FROM entity WHERE novel_id=$1`, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, `DELETE FROM graph_revision WHERE novel_id=$1`, `DELETE FROM novel WHERE id=$1`} {
			_, _ = admin.Exec(context.Background(), q, novelID)
		}
	})
	view, err := store.GetEntity(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, f := range view.Facts {
		seen[f.Attribute] = true
	}
	if !seen["description"] || !seen["old_description"] || seen["candidate_term"] {
		t.Fatalf("facts at 1=%+v", view.Facts)
	}
	view, err = store.GetEntity(ctx, novelID, a, 2)
	if err != nil {
		t.Fatal(err)
	}
	seen = map[string]bool{}
	for _, f := range view.Facts {
		seen[f.Attribute] = true
	}
	if !seen["description"] || seen["old_description"] {
		t.Fatalf("facts at 2=%+v", view.Facts)
	}
	var newFacts ChapterView
	if err := store.withReaderTx(ctx, novelID, 1, func(tx pgx.Tx) error { return newFactsInTx(ctx, tx, novelID, 1, &newFacts) }); err != nil {
		t.Fatal(err)
	}
	if !slices.ContainsFunc(newFacts.NewFacts, func(f ChapterFactView) bool { return f.Attribute == "old_description" }) {
		t.Fatalf("old label missing before alias chapter: %+v", newFacts.NewFacts)
	}
	newFacts = ChapterView{}
	if err := store.withReaderTx(ctx, novelID, 2, func(tx pgx.Tx) error { return newFactsInTx(ctx, tx, novelID, 1, &newFacts) }); err != nil {
		t.Fatal(err)
	}
	if slices.ContainsFunc(newFacts.NewFacts, func(f ChapterFactView) bool { return f.Attribute == "old_description" }) || !slices.ContainsFunc(newFacts.NewFacts, func(f ChapterFactView) bool { return f.Attribute == "description" }) {
		t.Fatalf("alias not canonicalized at chapter 2: %+v", newFacts.NewFacts)
	}
	rels, err := store.ListRelationships(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(rels) != 1 || rels[0].Relation != "ally" {
		t.Fatalf("relationships=%+v", rels)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel_vocabulary SET status='banned' WHERE novel_id=$1 AND term_type='relation' AND name='ally'`, novelID); err != nil {
		t.Fatal(err)
	}
	rels, err = store.ListRelationships(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(rels) != 0 {
		t.Fatalf("banned relationship visible: %+v", rels)
	}
	var raw string
	if err := admin.QueryRow(ctx, `SELECT rel_type FROM edge WHERE novel_id=$1`, novelID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if raw != "ally" {
		t.Fatalf("edge mutated: %q", raw)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel_vocabulary SET status='admitted' WHERE novel_id=$1 AND term_type='relation' AND name='ally'`, novelID); err != nil {
		t.Fatal(err)
	}
	var factCount, edgeCount int
	if err := admin.QueryRow(ctx, `SELECT count(*) FROM fact WHERE novel_id=$1`, novelID).Scan(&factCount); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRow(ctx, `SELECT count(*) FROM edge WHERE novel_id=$1`, novelID).Scan(&edgeCount); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO fact(novel_id,entity_id,attribute,value,valid_from_chapter,source_chapter,supersedes) VALUES($1,$2,'candidate_term','successor',0,1,$3)`, novelID, a, predecessorFact); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO edge(novel_id,src_id,dst_id,rel_type,valid_from_chapter,source_chapter,supersedes) VALUES($1,$2,$3,'candidate_relation',0,1,$4)`, novelID, a, b, predecessorEdge); err != nil {
		t.Fatal(err)
	}
	view, err = store.GetEntity(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.ContainsFunc(view.Facts, func(f FactView) bool { return f.Attribute == "description" }) {
		t.Fatalf("candidate successor hid predecessor: %+v", view.Facts)
	}
	rels, err = store.ListRelationships(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(rels) != 1 || rels[0].Relation != "ally" {
		t.Fatalf("candidate edge successor hid predecessor: %+v", rels)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel_vocabulary SET status='admitted' WHERE novel_id=$1 AND term_type='attribute' AND name='candidate_term'`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel_vocabulary SET status='admitted' WHERE novel_id=$1 AND term_type='relation' AND name='candidate_relation'`, novelID); err != nil {
		t.Fatal(err)
	}
	view, err = store.GetEntity(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if slices.ContainsFunc(view.Facts, func(f FactView) bool { return f.Attribute == "description" }) {
		t.Fatalf("admitted successor did not supersede predecessor: %+v", view.Facts)
	}
	rels, err = store.ListRelationships(ctx, novelID, a, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(rels) != 1 || rels[0].Relation != "candidate_relation" {
		t.Fatalf("admitted edge successor missing: %+v", rels)
	}
	var afterFacts, afterEdges int
	if err := admin.QueryRow(ctx, `SELECT count(*) FROM fact WHERE novel_id=$1`, novelID).Scan(&afterFacts); err != nil {
		t.Fatal(err)
	}
	if err := admin.QueryRow(ctx, `SELECT count(*) FROM edge WHERE novel_id=$1`, novelID).Scan(&afterEdges); err != nil {
		t.Fatal(err)
	}
	if factCount+1 != afterFacts || edgeCount+1 != afterEdges {
		t.Fatalf("knowledge row counts changed unexpectedly: facts %d->%d edges %d->%d", factCount, afterFacts, edgeCount, afterEdges)
	}
}

func TestNextExistsRegardlessOfTranslationStatusAndGlossaryDeletionIsHidden(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID := uuid.New().String()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES ($1,'Navigation test','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(ctx, `DELETE FROM glossary WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM chapter WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM novel WHERE id=$1`, novelID)
	})
	if _, err := admin.Exec(ctx, `INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status) VALUES ($1,2,'nav-test','test','{}','ingested')`, novelID); err != nil {
		t.Fatal(err)
	}
	for _, status := range []string{"ingested", "queued", "error", "done"} {
		if _, err := admin.Exec(ctx, `UPDATE chapter SET status=$1 WHERE novel_id=$2`, status, novelID); err != nil {
			t.Fatal(err)
		}
		if exists, err := store.hasNextChapter(ctx, novelID, 1); err != nil || !exists {
			t.Fatalf("next when %s: %v %v", status, exists, err)
		}
	}
	if exists, err := store.hasNextChapter(ctx, novelID, 2); err != nil || exists {
		t.Fatalf("nonexistent next: %v %v", exists, err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO glossary(novel_id,source_term,target_term,version,locked_at_chapter,deleted) VALUES ($1,'visible','Visible',1,0,false), ($1,'deleted','Deleted',2,0,true), ($1,'future','Future',3,100,false)`, novelID); err != nil {
		t.Fatal(err)
	}
	terms, err := store.ListGlossary(ctx, novelID, 0)
	if err != nil || len(terms) != 1 || terms[0].SourceTerm != "visible" {
		t.Fatalf("glossary: %v %v", terms, err)
	}
}

func TestReadableTranslationSurvivesGraphFailure(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES ($1,'Readiness test','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(ctx, `DELETE FROM reader_progress WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM chapter WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM novel WHERE id=$1`, novelID)
	})
	if _, err := admin.Exec(ctx, `INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta,status,translation_ready) VALUES ($1,1,'ready','raw','{}','error',true),($1,2,'failed','raw','{}','error',false)`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AdvanceProgress(ctx, "readiness-test", novelID, 1); err != nil {
		t.Fatalf("readable despite graph failure: %v", err)
	}
	if _, err := store.AdvanceProgress(ctx, "readiness-test", novelID, 2); err == nil {
		t.Fatal("failed translation became readable")
	}
	chapters, _, err := store.ListChapters(ctx, novelID, 10, 0)
	if err != nil || len(chapters) != 2 || chapters[0].Status != "done" || chapters[0].GraphStatus != "error" || chapters[1].Status != "error" {
		t.Fatalf("chapter statuses: %+v %v", chapters, err)
	}
	// A ready preview should neither need Redis nor return stale streaming text.
	_, available, status, failureCategory, err := store.TranslationPreview(ctx, novelID, 1)
	if err != nil || available || status != "done" || failureCategory != "" {
		t.Fatalf("preview: %v %s %s %v", available, status, failureCategory, err)
	}
}

func TestGlossaryEntityLinksRespectKnowledgeTime(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID, visibleID, futureID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES ($1,'Entity glossary test','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(ctx, `DELETE FROM glossary WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM entity WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		admin.Exec(ctx, `DELETE FROM novel WHERE id=$1`, novelID)
	})
	if _, err := admin.Exec(ctx, `INSERT INTO entity(id,novel_id,canonical,kind,first_seen_chapter) VALUES ($1,$3,'visible','character',1), ($2,$3,'future','character',10)`, visibleID, futureID, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO glossary(novel_id,source_term,target_term,entity_id,version,locked_at_chapter) VALUES ($1,'known-seed','Known',$2,1,0),($1,'later-seed','Later',$3,2,0),($1,'manual','Manual',NULL,3,0)`, novelID, visibleID, futureID); err != nil {
		t.Fatal(err)
	}
	terms, err := store.ListGlossary(ctx, novelID, 1)
	if err != nil || len(terms) != 3 {
		t.Fatalf("terms: %v %v", terms, err)
	}
	if terms[0].EntityID == nil || *terms[0].EntityID != visibleID {
		t.Fatal("visible entity link missing")
	}
	if terms[1].EntityID != nil || terms[2].EntityID != nil {
		t.Fatal("future entity ID leaked or unbound seed invented a link")
	}
	terms, err = store.ListGlossary(ctx, novelID, 10)
	if err != nil || terms[1].EntityID == nil || *terms[1].EntityID != futureID {
		t.Fatalf("later entity link: %v %v", terms, err)
	}
}

// TestChapterKnowledgeCanExtractReflectsTheRealGate is migration 0058's regression: a
// never-rebuilt novel's active revision is legacy=true, trusted=true (0023's
// initialize_graph_revision trigger), so `Trusted` alone used to say "writable" for a
// book that cannot take a per-chapter extraction at all, and said nothing during an
// unfinished rebuild beyond "not trusted" — the one state that most needs to name its
// cause and its escape (`discard`).
func TestChapterKnowledgeCanExtractReflectsTheRealGate(t *testing.T) {
	store, admin := integrationDatabase(t)
	fixture := seedIntegrationFixture(t, admin)
	ctx := context.Background()

	// A fresh novel: legacy, trusted, and — the bug this migration fixes — NOT
	// extractable, because KnowledgeEngine requires a pinned model a legacy revision
	// never recorded.
	view, err := store.ChapterKnowledge(ctx, fixture.novelID, 100, 220)
	if err != nil {
		t.Fatal(err)
	}
	if !view.Legacy || !view.Trusted || view.CanExtract || view.BlockedReason != "never_built" {
		t.Fatalf("fresh legacy novel: legacy=%v trusted=%v can_extract=%v blocked_reason=%q",
			view.Legacy, view.Trusted, view.CanExtract, view.BlockedReason)
	}

	var legacyRevision string
	if err := admin.QueryRow(ctx, `SELECT active_graph_revision::text FROM novel WHERE id=$1`, fixture.novelID).Scan(&legacyRevision); err != nil {
		t.Fatal(err)
	}

	// prepare()'s own quarantine: the active revision goes untrusted the instant a
	// rebuild starts, with nothing published yet on its replacement.
	if _, err := admin.Exec(ctx, `UPDATE graph_revision SET trusted=false WHERE id=$1`, legacyRevision); err != nil {
		t.Fatal(err)
	}
	view, err = store.ChapterKnowledge(ctx, fixture.novelID, 100, 220)
	if err != nil {
		t.Fatal(err)
	}
	if view.Trusted || view.CanExtract || view.BlockedReason != "quarantined" {
		t.Fatalf("quarantined novel: trusted=%v can_extract=%v blocked_reason=%q",
			view.Trusted, view.CanExtract, view.BlockedReason)
	}

	// An activated managed revision with this chapter in its snapshot: the one case
	// per-chapter extraction is actually permitted.
	managedRevision := uuid.NewString()
	if _, err := admin.Exec(ctx, `UPDATE graph_revision SET state='archived' WHERE id=$1`, legacyRevision); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `INSERT INTO graph_revision(id,novel_id,state,trusted,legacy,ontology,model,snapshot)
		VALUES($1,$2,'active',true,false,'{}','{"provider":"ollama","name":"test"}',$3)`,
		managedRevision, fixture.novelID, `{"chapters":[{"chapter":100,"source_hash":"h"}]}`); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=$1 WHERE id=$2`, managedRevision, fixture.novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(context.Background(), `UPDATE novel SET active_graph_revision=$1 WHERE id=$2`, legacyRevision, fixture.novelID)
		admin.Exec(context.Background(), `DELETE FROM graph_revision WHERE id=$1`, managedRevision)
		admin.Exec(context.Background(), `UPDATE graph_revision SET state='active',trusted=true WHERE id=$1`, legacyRevision)
	})

	view, err = store.ChapterKnowledge(ctx, fixture.novelID, 100, 220)
	if err != nil {
		t.Fatal(err)
	}
	if view.Legacy || !view.Trusted || !view.ChapterSnapshotted || !view.CanExtract || view.BlockedReason != "" {
		t.Fatalf("managed activated revision: legacy=%v trusted=%v snapshotted=%v can_extract=%v blocked_reason=%q",
			view.Legacy, view.Trusted, view.ChapterSnapshotted, view.CanExtract, view.BlockedReason)
	}

	// The same managed revision, asked about a chapter it never snapshotted.
	view, err = store.ChapterKnowledge(ctx, fixture.novelID, 220, 220)
	if err != nil {
		t.Fatal(err)
	}
	if view.ChapterSnapshotted || view.CanExtract || view.BlockedReason != "chapter_not_snapshotted" {
		t.Fatalf("un-snapshotted chapter: snapshotted=%v can_extract=%v blocked_reason=%q",
			view.ChapterSnapshotted, view.CanExtract, view.BlockedReason)
	}
}

// Deleting a graph intentionally leaves active_graph_revision NULL. Chapters and
// glossary data remain readable; only graph-backed rows disappear behind RLS.
func TestKnowledgeStatusWithoutGraphIsUnavailableNotAnError(t *testing.T) {
	store, admin := integrationDatabase(t)
	ctx := context.Background()
	novelID := uuid.NewString()
	if _, err := admin.Exec(ctx, `INSERT INTO novel(id,title,source_lang,target_lang,ontology)
		VALUES ($1,'Deleted graph test','zh','en','{}')`, novelID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(context.Background(), `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID)
		admin.Exec(context.Background(), `DELETE FROM graph_revision WHERE novel_id=$1`, novelID)
		admin.Exec(context.Background(), `DELETE FROM novel WHERE id=$1`, novelID)
	})
	if _, err := admin.Exec(ctx, `UPDATE novel SET active_graph_revision=NULL WHERE id=$1`, novelID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, `DELETE FROM graph_revision WHERE novel_id=$1`, novelID); err != nil {
		t.Fatal(err)
	}

	status, err := store.KnowledgeStatus(ctx, novelID, 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	if status.Status != "unavailable" || status.RevisionID != "" || status.Trusted || status.CanExtract {
		t.Fatalf("status without graph = %#v", status)
	}
	if reason := blockedReason(status); reason != "never_built" {
		t.Fatalf("blocked reason = %q, want never_built", reason)
	}
}
