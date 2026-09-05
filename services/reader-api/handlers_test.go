package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const (
	testNovelID  = "11111111-1111-4111-8111-111111111111"
	testEntityID = "22222222-2222-4222-8222-222222222222"
)

type fakeStore struct {
	healthErr       error
	progress        Progress
	progressErr     error
	advance         Progress
	advanceErr      error
	entity          EntityView
	entityErr       error
	wiki            []EntitySummary
	wikiErr         error
	timeline        []EventView
	timelineErr     error
	relationships   []RelationshipView
	relationshipErr error
	chapter         ChapterView
	chapterErr      error
	novels          []NovelSummary
	novelsErr       error
	novel           NovelSummary
	novelErr        error
	scrapeJobID     int64
	createScrapeErr error
	scrapeJob       ScrapeJobView
	scrapeJobErr    error
	cancelErr       error
	glossary        []GlossaryTermView
	glossaryErr     error
	lastAt          int
	lastReader      string
	lastChapter     int
	lastChapterArg  int

	chapterList       []ChapterListItem
	chapterListTotal  int
	chapterListErr    error
	lastChapterLimit  int
	lastChapterOffset int
	pipelineStatusErr error
	previewText       string
	previewStatus     string
	previewErr        error
	health            TranslationHealth
	healthErr2        error
	repair            RepairStatus
	repairErr         error
	repairPreview     RepairPreview
	repairPreviewErr  error
	repairProgress    []RepairProgressFact
	repairProgressErr error
}

type fakeIngestClient struct {
	response json.RawMessage
	status   int
	err      error
	lastBody json.RawMessage
}

func (f *fakeIngestClient) QueueControl(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) CreateNovel(_ context.Context, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) DeleteNovel(_ context.Context, _ string) (json.RawMessage, int, error) {
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) PasteChapter(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) CorrectGlossaryTerm(_ context.Context, _, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) DeleteGlossaryTerm(_ context.Context, _, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) ListProviderCredentials(_ context.Context) (json.RawMessage, int, error) {
	return json.RawMessage(`{"credentials":[]}`), 200, nil
}

func (f *fakeIngestClient) PutProviderCredential(_ context.Context, _ string, _ json.RawMessage) (json.RawMessage, int, error) {
	return json.RawMessage(`{"credentials":[]}`), 200, nil
}

func (f *fakeIngestClient) DeleteProviderCredential(_ context.Context, _ string) (json.RawMessage, int, error) {
	return nil, 204, nil
}

func (f *fakeIngestClient) GetProviderConfig(_ context.Context, _ string) (json.RawMessage, int, error) {
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) ListOllamaModels(_ context.Context, _ string) (json.RawMessage, int, error) {
	return json.RawMessage(`{"models":["qwen2.5:7b-instruct"]}`), http.StatusOK, nil
}

func (f *fakeIngestClient) PutProviderConfig(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) BootstrapGlossary(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) ConfirmGlossaryTerm(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) ApproveCharacterName(_ context.Context, _, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) RequestRepair(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) CancelRepair(_ context.Context, _, _ string) (json.RawMessage, int, error) {
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) TranslateAhead(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) UpdateNovelSettings(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

type fakeAskClient struct {
	response json.RawMessage
	err      error
	novelID  string
	question string
	at       int
}

func (f *fakeAskClient) Ask(_ context.Context, novelID, question string, at int) (json.RawMessage, error) {
	f.novelID, f.question, f.at = novelID, question, at
	return f.response, f.err
}

func (f *fakeStore) Health(context.Context) error { return f.healthErr }

func (f *fakeStore) GetProgress(
	_ context.Context, readerID, _ string,
) (Progress, error) {
	f.lastReader = readerID
	return f.progress, f.progressErr
}

func (f *fakeStore) AdvanceProgress(
	_ context.Context, readerID, _ string, chapter int,
) (Progress, error) {
	f.lastReader = readerID
	f.lastChapter = chapter
	return f.advance, f.advanceErr
}

func (f *fakeStore) GetEntity(
	_ context.Context, _, _ string, at int,
) (EntityView, error) {
	f.lastAt = at
	return f.entity, f.entityErr
}

func (f *fakeStore) ListWiki(
	_ context.Context, _ string, at int,
) ([]EntitySummary, error) {
	f.lastAt = at
	return f.wiki, f.wikiErr
}

func (f *fakeStore) ListTimeline(
	_ context.Context, _ string, at int,
) ([]EventView, error) {
	f.lastAt = at
	return f.timeline, f.timelineErr
}

func (f *fakeStore) EventStatus(context.Context, string, int, int) (KnowledgeStatus, error) {
	return KnowledgeStatus{RevisionID: "events", Version: 1, Trusted: true, Status: "done"}, nil
}

func (f *fakeStore) ListRelationships(
	_ context.Context, _, _ string, at int,
) ([]RelationshipView, error) {
	f.lastAt = at
	return f.relationships, f.relationshipErr
}

func (f *fakeStore) ListNovels(context.Context) ([]NovelSummary, error) {
	return f.novels, f.novelsErr
}

func (f *fakeStore) GetNovel(context.Context, string) (NovelSummary, error) {
	return f.novel, f.novelErr
}

func (f *fakeStore) CreateScrapeJob(context.Context, string, string, string) (int64, error) {
	return f.scrapeJobID, f.createScrapeErr
}

func (f *fakeStore) LatestScrapeJob(context.Context, string) (ScrapeJobView, error) {
	return f.scrapeJob, f.scrapeJobErr
}

func (f *fakeStore) RequestScrapeCancel(context.Context, string) error {
	return f.cancelErr
}

func (f *fakeStore) ListGlossary(_ context.Context, _ string, at int) ([]GlossaryTermView, error) {
	f.lastAt = at
	return f.glossary, f.glossaryErr
}

func (f *fakeStore) ListNameReviews(context.Context, string, *int) ([]CharacterNameReview, error) {
	return []CharacterNameReview{}, nil
}

func (f *fakeStore) GetChapter(
	_ context.Context, _ string, n, at int,
) (ChapterView, error) {
	f.lastChapterArg = n
	return f.chapter, f.chapterErr
}

func (f *fakeStore) ListChapters(
	_ context.Context, _ string, limit, offset int,
) ([]ChapterListItem, int, error) {
	f.lastChapterLimit, f.lastChapterOffset = limit, offset
	return f.chapterList, f.chapterListTotal, f.chapterListErr
}

func (f *fakeStore) PipelineStatus(_ context.Context, novelID string) (PipelineStatusResponse, error) {
	return PipelineStatusResponse{NovelID: novelID, InFlight: []InFlightChapter{}}, f.pipelineStatusErr
}

func (f *fakeStore) TranslationPreview(_ context.Context, _ string, _ int) (string, bool, string, error) {
	return f.previewText, f.previewText != "", f.previewStatus, f.previewErr
}

func (f *fakeStore) TranslationHealth(_ context.Context, novelID string) (TranslationHealth, error) {
	health := f.health
	health.NovelID = novelID
	return health, f.healthErr2
}

func (f *fakeStore) RepairPreview(_ context.Context, _, _ string) (RepairPreview, error) {
	return f.repairPreview, f.repairPreviewErr
}

func (f *fakeStore) RepairProgress(_ context.Context, _ string) ([]RepairProgressFact, error) {
	return f.repairProgress, f.repairProgressErr
}

func (f *fakeStore) RepairExtraction(_ context.Context, _ string) ([]RepairExtractedName, error) {
	return nil, nil
}

func (f *fakeStore) RepairStatus(_ context.Context, novelID string) (RepairStatus, error) {
	status := f.repair
	status.NovelID = novelID
	return status, f.repairErr
}

func request(t *testing.T, api *API, method, target, body, reader string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	if reader != "" {
		req.Header.Set("X-Reader-ID", reader)
	}
	recorder := httptest.NewRecorder()
	api.routes().ServeHTTP(recorder, req)
	return recorder
}

func readyFake() *fakeStore {
	entityID := testEntityID
	return &fakeStore{
		progress: Progress{
			NovelID: testNovelID, ReaderID: "reader-a", CurrentChapter: 5, UpdatedAt: time.Now(),
		},
		advance: Progress{
			NovelID: testNovelID, ReaderID: "reader-a", CurrentChapter: 5, UpdatedAt: time.Now(),
		},
		entity: EntityView{
			EntitySummary: EntitySummary{
				ID: testEntityID, Canonical: "Hero", Kind: "character", FirstSeenChapter: 1,
			},
			Aliases: []string{}, Facts: []FactView{},
		},
		wiki:          []EntitySummary{},
		timeline:      []EventView{},
		relationships: []RelationshipView{},
		chapter: ChapterView{
			Text:      "chapter text",
			Spans:     []SpanView{{EntityID: &entityID, CharStart: 0, CharEnd: 7}},
			HasNext:   true,
			SourceURL: "https://example.com/novel/chapter-3",
		},
		novels: []NovelSummary{{ID: testNovelID, Title: "Test Novel", SourceLang: "zh", TargetLang: "en"}},
		novel:  NovelSummary{ID: testNovelID, Title: "Test Novel", SourceLang: "zh", TargetLang: "en"},
	}
}

func TestReaderRoutesRequirePrincipal(t *testing.T) {
	api := &API{store: readyFake()}
	response := request(t, api, http.MethodGet,
		"/novels/"+testNovelID+"/wiki", "", "")

	if response.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body=%s", response.Code, response.Body.String())
	}
	if response.Header().Get("Cache-Control") != "private, no-store" {
		t.Fatalf("missing private cache control: %q", response.Header().Get("Cache-Control"))
	}
}

func TestGateValidation(t *testing.T) {
	tests := []struct {
		name   string
		target string
	}{
		{name: "bad novel", target: "/novels/not-a-uuid/wiki"},
		{name: "negative at", target: "/novels/" + testNovelID + "/wiki?at=-1"},
		{name: "malformed at", target: "/novels/" + testNovelID + "/wiki?at=later"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			response := request(t, &API{store: readyFake()}, http.MethodGet, test.target, "", "r")
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%s", response.Code, response.Body.String())
			}
		})
	}
}

func TestRequestedChapterIsCappedAtProgress(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/wiki?at=500", "", " reader-a ")

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d; body=%s", response.Code, response.Body.String())
	}
	if store.lastAt != 5 || store.lastReader != "reader-a" {
		t.Fatalf("gate = (%q, %d), want (reader-a, 5)", store.lastReader, store.lastAt)
	}
	var body WikiResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.At != 5 || body.Entities == nil {
		t.Fatalf("response = %#v, want effective at and non-null collection", body)
	}
}

func TestLowerRequestedChapterIsUsed(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/entity/"+testEntityID+"?at=2", "", "reader-a")
	if response.Code != http.StatusOK || store.lastAt != 2 {
		t.Fatalf("status=%d at=%d body=%s", response.Code, store.lastAt, response.Body.String())
	}
}

func TestCollectionEndpointsReturnStableEnvelopes(t *testing.T) {
	store := readyFake()
	store.timeline = []EventView{{ID: "00000000-0000-0000-0000-000000000001", ChapterIndex: 2, Summary: "Event", Entities: []EntitySummary{}, Arguments: []EventArgumentView{}}}
	store.relationships = []RelationshipView{{
		ID: 1, Relation: "ally", Direction: "outgoing",
		Entity: EntitySummary{ID: testEntityID, Canonical: "Ally", Kind: "character"},
	}}
	tests := []struct {
		name   string
		target string
	}{
		{name: "timeline", target: "/novels/" + testNovelID + "/timeline"},
		{name: "relationships", target: "/novels/" + testNovelID + "/relationships/" + testEntityID},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			response := request(t, &API{store: store}, http.MethodGet, test.target, "", "reader-a")
			if response.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
			var envelope map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope["novel_id"] != testNovelID || envelope["at"] != float64(5) {
				t.Fatalf("envelope = %#v", envelope)
			}
		})
	}
}

func TestMissingProgressAndHiddenEntityAreNotFound(t *testing.T) {
	missingProgress := readyFake()
	missingProgress.progressErr = ErrNotFound
	response := request(t, &API{store: missingProgress}, http.MethodGet,
		"/novels/"+testNovelID+"/wiki", "", "reader-a")
	if response.Code != http.StatusNotFound {
		t.Fatalf("missing progress status = %d", response.Code)
	}

	hidden := readyFake()
	hidden.entityErr = ErrNotFound
	response = request(t, &API{store: hidden}, http.MethodGet,
		"/novels/"+testNovelID+"/entity/"+testEntityID, "", "reader-a")
	if response.Code != http.StatusNotFound {
		t.Fatalf("hidden entity status = %d", response.Code)
	}
}

func TestPutProgressContract(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodPut,
		"/novels/"+testNovelID+"/progress", `{"chapter":5}`, "reader-a")
	if response.Code != http.StatusOK || store.lastChapter != 5 {
		t.Fatalf("status=%d chapter=%d body=%s", response.Code, store.lastChapter, response.Body.String())
	}

	store.advanceErr = ErrChapterNotReady
	response = request(t, &API{store: store}, http.MethodPut,
		"/novels/"+testNovelID+"/progress", `{"chapter":6}`, "reader-a")
	if response.Code != http.StatusConflict {
		t.Fatalf("not-ready status = %d, want 409", response.Code)
	}
}

func TestPutProgressRejectsInvalidBodies(t *testing.T) {
	for _, body := range []string{
		`{}`,
		`{"chapter":-1}`,
		`{"chapter":1,"extra":true}`,
		`{"chapter":1}{"chapter":2}`,
		`not-json`,
	} {
		response := request(t, &API{store: readyFake()}, http.MethodPut,
			"/novels/"+testNovelID+"/progress", body, "reader-a")
		if response.Code != http.StatusBadRequest {
			t.Fatalf("body %q status = %d, want 400", body, response.Code)
		}
	}
}

func TestAskUsesEffectiveGateAndMapsAvailability(t *testing.T) {
	store := readyFake()
	ask := &fakeAskClient{response: json.RawMessage(`{"answer":"safe","at":5,"retrieved_sources":[],"served_by":null}`)}
	api := &API{store: store, ask: ask}
	response := request(t, api, http.MethodPost, "/novels/"+testNovelID+"/ask", `{"question":"What happened?","at":500}`, "reader-a")
	if response.Code != http.StatusOK || ask.at != 5 || ask.novelID != testNovelID {
		t.Fatalf("status=%d gate=(%s,%d) body=%s", response.Code, ask.novelID, ask.at, response.Body.String())
	}
	if response.Header().Get("Cache-Control") != "private, no-store" {
		t.Fatal("ask response was cacheable")
	}

	ask.err = ErrAskRejected
	response = request(t, api, http.MethodPost, "/novels/"+testNovelID+"/ask", `{"question":"What happened?"}`, "reader-a")
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("rejected status=%d", response.Code)
	}
}

func TestStoreErrorsMapToInternalServerError(t *testing.T) {
	store := readyFake()
	store.wikiErr = errors.New("database broke")
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/wiki", "", "reader-a")
	if response.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", response.Code)
	}
}

func TestGetChapterWithinProgressSucceeds(t *testing.T) {
	store := readyFake() // progress.CurrentChapter == 5
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapter/3", "", "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	if store.lastChapterArg != 3 {
		t.Fatalf("store called with chapter %d, want 3", store.lastChapterArg)
	}
	var body ChapterResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	// At must be the reader's STORED PROGRESS (5), not the requested chapter (3) — the
	// client uses this exact value as the hover-card cache key (PLAN.md §5.3/§6.1).
	if body.At != 5 || body.ChapterIndex != 3 || body.Text != "chapter text" || !body.HasNext ||
		body.SourceURL != "https://example.com/novel/chapter-3" {
		t.Fatalf("response = %#v", body)
	}
}

func TestGetChapterBeyondProgressIsNotFound(t *testing.T) {
	store := readyFake() // progress.CurrentChapter == 5
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapter/6", "", "reader-a")
	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", response.Code, response.Body.String())
	}
}

func TestGetChapterIncludesUnlinkedMentions(t *testing.T) {
	store := readyFake()
	store.chapter.Spans = append(store.chapter.Spans, SpanView{CharStart: 8, CharEnd: 12})
	api := &API{store: store}
	response := request(t, api, http.MethodGet, "/novels/"+testNovelID+"/chapter/3", "", "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d; body=%s", response.Code, response.Body.String())
	}
	var body ChapterResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Spans) != 2 || body.Spans[1].EntityID != nil || body.Spans[0].EntityID == nil {
		t.Fatalf("linked and unlinked mentions must both survive: %#v", body.Spans)
	}
}

func TestGetChapterIncludesReadableTranslationWarning(t *testing.T) {
	store := readyFake()
	store.chapter.TranslationWarning = &TranslationWarning{Code: "locked_terms_missing", TermCount: 2}
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapter/3", "", "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	var body ChapterResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.TranslationWarning == nil || body.TranslationWarning.TermCount != 2 {
		t.Fatalf("warning=%#v", body.TranslationWarning)
	}
}

func TestGetChapterInvalidIndexIsBadRequest(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapter/-1", "", "reader-a")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", response.Code, response.Body.String())
	}
}

func TestGetChapterMapsChapterNotReady(t *testing.T) {
	store := readyFake()
	store.chapterErr = ErrChapterNotReady
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapter/1", "", "reader-a")
	if response.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%s", response.Code, response.Body.String())
	}
}

func TestGetChaptersClampsLimitAndReportsProgress(t *testing.T) {
	store := readyFake()
	store.chapterList = []ChapterListItem{
		{ChapterIndex: 1, SiteChapterNo: "第4610章 帝一！", Status: "done"},
		{ChapterIndex: 2, Status: "ingested"},
	}
	store.chapterListTotal = 29

	// limit above maxChapterPage must clamp rather than be honoured verbatim — an
	// unbounded page over a several-thousand-chapter novel is the thing this guards.
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapters?limit=9999&offset=10", "", "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	if store.lastChapterLimit != maxChapterPage {
		t.Fatalf("limit = %d, want clamped to %d", store.lastChapterLimit, maxChapterPage)
	}
	if store.lastChapterOffset != 10 {
		t.Fatalf("offset = %d, want 10", store.lastChapterOffset)
	}

	var body ChapterListResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Total != 29 {
		t.Fatalf("total = %d, want 29", body.Total)
	}
	if body.Progress != 5 {
		t.Fatalf("progress = %d, want 5 (readyFake's stored progress)", body.Progress)
	}
	if len(body.Chapters) != 2 || body.Chapters[1].Status != "ingested" {
		t.Fatalf("chapters = %+v", body.Chapters)
	}
}

// A brand-new reader has no reader_progress row, but the chapter index is exactly what
// they need to pick a starting chapter — so it must report progress 0, not 404 the way
// every gated endpoint does.
func TestGetChaptersWithoutProgressRowStillLists(t *testing.T) {
	store := readyFake()
	store.progressErr = ErrNotFound
	store.chapterList = []ChapterListItem{{ChapterIndex: 1, Status: "done"}}
	store.chapterListTotal = 1

	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/chapters", "", "reader-new")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	var body ChapterListResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Progress != 0 {
		t.Fatalf("progress = %d, want 0", body.Progress)
	}
}

func TestGetNovelsRequiresNoPrincipal(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodGet, "/novels", "", "")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	var body NovelListResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Novels) != 1 || body.Novels[0].ID != testNovelID {
		t.Fatalf("response = %#v", body)
	}
}

func TestGetNovelNotFound(t *testing.T) {
	store := readyFake()
	store.novelErr = ErrNotFound
	response := request(t, &API{store: store}, http.MethodGet, "/novels/"+testNovelID, "", "")
	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", response.Code, response.Body.String())
	}
}

func TestPostNovelProxiesToIngestClient(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"id":"` + testNovelID + `"}`), status: http.StatusCreated}
	api := &API{store: readyFake(), ingest: ingest}
	response := request(t, api, http.MethodPost, "/novels", `{"title":"New Novel"}`, "")
	if response.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body=%s", response.Code, response.Body.String())
	}
	if string(ingest.lastBody) != `{"title":"New Novel"}` {
		t.Fatalf("body forwarded = %q", ingest.lastBody)
	}
	if !strings.Contains(response.Body.String(), testNovelID) {
		t.Fatalf("response body = %s", response.Body.String())
	}
}

func TestQueueControlProxiesSettingsAndErrors(t *testing.T) {
	for _, method := range []string{http.MethodGet, http.MethodPatch} {
		ingest := &fakeIngestClient{response: json.RawMessage(`{"mode":"paused","books":[]}`), status: 200}
		api := &API{store: readyFake(), ingest: ingest}
		body := ""
		if method == http.MethodPatch {
			body = `{"mode":"paused"}`
		}
		response := request(t, api, method, "/queue", body, "reader-1")
		if response.Code != 200 {
			t.Fatalf("proxy: %d %s", response.Code, response.Body)
		}
		if method == http.MethodGet && string(ingest.lastBody) != body {
			t.Fatalf("GET body forwarded = %q", ingest.lastBody)
		}
		if method == http.MethodPatch {
			var forwarded map[string]string
			if err := json.Unmarshal(ingest.lastBody, &forwarded); err != nil || forwarded["mode"] != "paused" || forwarded["changed_by"] != "reader-1" {
				t.Fatalf("PATCH body = %q, err=%v", ingest.lastBody, err)
			}
		}
		ingest.err = ErrIngestUnavailable
		if got := request(t, api, method, "/queue", body, ""); got.Code != 502 {
			t.Fatalf("status = %d", got.Code)
		}
	}
}

func TestPostNovelMapsIngestUnavailable(t *testing.T) {
	ingest := &fakeIngestClient{err: ErrIngestUnavailable}
	api := &API{store: readyFake(), ingest: ingest}
	response := request(t, api, http.MethodPost, "/novels", `{"title":"New Novel"}`, "")
	if response.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502; body=%s", response.Code, response.Body.String())
	}
}

func TestDeleteNovelProxiesToIngestClient(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"deleted":true}`), status: http.StatusOK}
	api := &API{store: readyFake(), ingest: ingest}
	response := request(t, api, http.MethodDelete, "/novels/"+testNovelID, "", "")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), `"deleted":true`) {
		t.Fatalf("response body = %s", response.Body.String())
	}
}

func TestDeleteNovelRejectsBadID(t *testing.T) {
	api := &API{store: readyFake(), ingest: &fakeIngestClient{}}
	response := request(t, api, http.MethodDelete, "/novels/not-a-uuid", "", "")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", response.Code, response.Body.String())
	}
}

func TestPostChapterProxiesToIngestClient(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"status":"ingested"}`), status: http.StatusAccepted}
	api := &API{store: readyFake(), ingest: ingest}
	response := request(t, api, http.MethodPost, "/novels/"+testNovelID+"/chapters", `{"chapter_index":1,"raw_text":"hi"}`, "")
	if response.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body=%s", response.Code, response.Body.String())
	}
	if string(ingest.lastBody) != `{"chapter_index":1,"raw_text":"hi"}` {
		t.Fatalf("body forwarded = %q", ingest.lastBody)
	}
}

func TestPostScrapeValidatesURLAndMode(t *testing.T) {
	tests := []struct {
		name string
		body string
	}{
		{"not a url", `{"start_url":"not-a-url"}`},
		{"relative url", `{"start_url":"/chapter-1"}`},
		{"bad scheme", `{"start_url":"ftp://example.com/chapter-1"}`},
		{"bad mode", `{"start_url":"https://example.com/chapter-1","mode":"bogus"}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			response := request(t, &API{store: readyFake()}, http.MethodPost,
				"/novels/"+testNovelID+"/scrape", test.body, "")
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%s", response.Code, response.Body.String())
			}
		})
	}
}

func TestPostScrapeStartsJob(t *testing.T) {
	store := readyFake()
	store.scrapeJobID = 42
	response := request(t, &API{store: store}, http.MethodPost,
		"/novels/"+testNovelID+"/scrape", `{"start_url":"https://freewebnovel.com/novel/x/chapter-1"}`, "")
	if response.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body=%s", response.Code, response.Body.String())
	}
	var body map[string]int64
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["id"] != 42 {
		t.Fatalf("response = %#v", body)
	}
}

func TestPostScrapeConflictWhenAlreadyActive(t *testing.T) {
	store := readyFake()
	store.createScrapeErr = ErrScrapeJobActive
	response := request(t, &API{store: store}, http.MethodPost,
		"/novels/"+testNovelID+"/scrape", `{"start_url":"https://freewebnovel.com/novel/x/chapter-1"}`, "")
	if response.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%s", response.Code, response.Body.String())
	}
}

func TestGetScrapeStatusNotFound(t *testing.T) {
	store := readyFake()
	store.scrapeJobErr = ErrNotFound
	response := request(t, &API{store: store}, http.MethodGet, "/novels/"+testNovelID+"/scrape/status", "", "")
	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", response.Code, response.Body.String())
	}
}

func TestPostScrapeCancel(t *testing.T) {
	response := request(t, &API{store: readyFake()}, http.MethodPost,
		"/novels/"+testNovelID+"/scrape/cancel", "", "")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
}

func TestGetGlossaryUsesGate(t *testing.T) {
	store := readyFake() // progress.CurrentChapter == 5
	store.glossary = []GlossaryTermView{{SourceTerm: "青云宗", TargetTerm: "Azure Cloud Sect", Version: 1, LockedAtChapter: 1}}
	response := request(t, &API{store: store}, http.MethodGet, "/novels/"+testNovelID+"/glossary", "", "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	var body GlossaryResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.At != 5 || len(body.Terms) != 1 {
		t.Fatalf("response = %#v", body)
	}
}

func TestGetGlossaryRequiresPrincipal(t *testing.T) {
	response := request(t, &API{store: readyFake()}, http.MethodGet, "/novels/"+testNovelID+"/glossary", "", "")
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body=%s", response.Code, response.Body.String())
	}
}

func TestPatchGlossaryTermRequiresPrincipal(t *testing.T) {
	response := request(t, &API{store: readyFake(), ingest: &fakeIngestClient{}}, http.MethodPatch,
		"/novels/"+testNovelID+"/glossary/%E9%9D%92%E4%BA%91%E5%AE%97", `{"target_term":"x","at_chapter":1}`, "")
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401; body=%s", response.Code, response.Body.String())
	}
}

func TestPatchGlossaryTermProxiesToIngestClient(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"version":2}`), status: http.StatusOK}
	response := request(t, &API{store: readyFake(), ingest: ingest}, http.MethodPatch,
		"/novels/"+testNovelID+"/glossary/%E9%9D%92%E4%BA%91%E5%AE%97", `{"target_term":"Verdant Cloud Sect","at_chapter":1}`, "reader-a")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", response.Code, response.Body.String())
	}
	if string(ingest.lastBody) != `{"target_term":"Verdant Cloud Sect","at_chapter":1}` {
		t.Fatalf("body forwarded = %q", ingest.lastBody)
	}
}

func TestConfirmGlossaryTermRequiresPrincipalAndProxiesBody(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"version":2}`), status: http.StatusCreated}
	api := &API{store: readyFake(), ingest: ingest}
	path := "/novels/" + testNovelID + "/glossary/confirm"
	body := `{"source_term":"契科夫","target_term":"Chekhov","at_chapter":5,"term_role":"foreign_person"}`
	if response := request(t, api, http.MethodPost, path, body, ""); response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated confirm: %d", response.Code)
	}
	response := request(t, api, http.MethodPost, path, body, "reader-a")
	if response.Code != http.StatusCreated || string(ingest.lastBody) != body {
		t.Fatalf("confirm response: %d %s; forwarded=%s", response.Code, response.Body.String(), ingest.lastBody)
	}
}

func TestHealthChecksBothPoolsThroughStore(t *testing.T) {
	store := readyFake()
	response := request(t, &API{store: store}, http.MethodGet, "/healthz", "", "")
	if response.Code != http.StatusOK {
		t.Fatalf("healthy status = %d", response.Code)
	}
	store.healthErr = errors.New("down")
	response = request(t, &API{store: store}, http.MethodGet, "/healthz", "", "")
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("unhealthy status = %d", response.Code)
	}
}

func TestGlossaryBeforeReadingExposesOnlyChapterZero(t *testing.T) {
	store := readyFake()
	store.progressErr = ErrNotFound
	response := request(t, &API{store: store}, http.MethodGet, "/novels/"+testNovelID+"/glossary?at=999", "", "new-reader")
	var body GlossaryResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || body.At != 0 {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestDeleteGlossaryRequiresPrincipalAndProxiesBody(t *testing.T) {
	ingest := &fakeIngestClient{response: json.RawMessage(`{"deleted":true,"version":3}`), status: http.StatusOK}
	api := &API{store: readyFake(), ingest: ingest}
	path := "/novels/" + testNovelID + "/glossary/%E5%87%8C%E5%B3%B0"
	if response := request(t, api, http.MethodDelete, path, `{"at_chapter":2}`, ""); response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated delete: %d", response.Code)
	}
	response := request(t, api, http.MethodDelete, path, `{"at_chapter":2}`, "reader-a")
	if response.Code != http.StatusOK || string(ingest.lastBody) != `{"at_chapter":2}` {
		t.Fatalf("delete response: %d %s", response.Code, response.Body.String())
	}
}

func (f *fakeStore) KnowledgeStatus(context.Context, string, int, int) (KnowledgeStatus, error) {
	return KnowledgeStatus{RevisionID: "test", Version: 1, Trusted: true, Status: "done"}, nil
}

func TestKnowledgeStatusUsesStoredProgress(t *testing.T) {
	api := &API{store: readyFake()}
	for _, tc := range []struct {
		chapter string
		status  int
	}{{"2", 200}, {"5", 200}, {"6", 404}, {"-1", 400}, {"bad", 400}} {
		response := request(t, api, http.MethodGet, "/novels/"+testNovelID+"/knowledge-status?chapter="+tc.chapter, "", "reader-a")
		if response.Code != tc.status {
			t.Fatalf("chapter=%s status=%d body=%s", tc.chapter, response.Code, response.Body.String())
		}
		if tc.status == 200 && !strings.Contains(response.Body.String(), `"revision_id":"test"`) {
			t.Fatal("missing revision metadata")
		}
	}
}
