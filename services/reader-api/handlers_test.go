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
	lastAt          int
	lastReader      string
	lastChapter     int
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

func TestStoreErrorsMapToInternalServerError(t *testing.T) {
	store := readyFake()
	store.wikiErr = errors.New("database broke")
	response := request(t, &API{store: store}, http.MethodGet,
		"/novels/"+testNovelID+"/wiki", "", "reader-a")
	if response.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", response.Code)
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
