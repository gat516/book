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
	lastAt          int
	lastReader      string
	lastChapter     int
	lastChapterArg  int
}

type fakeIngestClient struct {
	response json.RawMessage
	status   int
	err      error
	lastBody json.RawMessage
}

func (f *fakeIngestClient) CreateNovel(_ context.Context, body json.RawMessage) (json.RawMessage, int, error) {
	f.lastBody = body
	return f.response, f.status, f.err
}

func (f *fakeIngestClient) PasteChapter(_ context.Context, _ string, body json.RawMessage) (json.RawMessage, int, error) {
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

func (f *fakeStore) GetChapter(
	_ context.Context, _ string, n int,
) (ChapterView, error) {
	f.lastChapterArg = n
	return f.chapter, f.chapterErr
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
			Text:    "chapter text",
			Spans:   []SpanView{{EntityID: testEntityID, CharStart: 0, CharEnd: 7}},
			HasNext: true,
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
	store.timeline = []EventView{{ID: 1, ChapterIndex: 2, Summary: "Event", Entities: []EntitySummary{}}}
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
	if body.At != 5 || body.ChapterIndex != 3 || body.Text != "chapter text" || !body.HasNext {
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

func TestPostNovelMapsIngestUnavailable(t *testing.T) {
	ingest := &fakeIngestClient{err: ErrIngestUnavailable}
	api := &API{store: readyFake(), ingest: ingest}
	response := request(t, api, http.MethodPost, "/novels", `{"title":"New Novel"}`, "")
	if response.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502; body=%s", response.Code, response.Body.String())
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
