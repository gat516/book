package main

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
)

type API struct {
	store  ReaderStore
	ask    AskClient
	ingest IngestClient
}

type progressRequest struct {
	Chapter *int `json:"chapter"`
}

func (a *API) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", a.healthz)
	mux.HandleFunc("PUT /novels/{id}/progress", a.putProgress)
	mux.HandleFunc("GET /novels/{id}/entity/{eid}", a.getEntity)
	mux.HandleFunc("GET /novels/{id}/wiki", a.getWiki)
	mux.HandleFunc("GET /novels/{id}/timeline", a.getTimeline)
	mux.HandleFunc("GET /novels/{id}/relationships/{eid}", a.getRelationships)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}", a.getChapter)
	mux.HandleFunc("POST /novels/{id}/ask", a.postAsk)
	mux.HandleFunc("GET /novels", a.getNovels)
	mux.HandleFunc("GET /novels/{id}", a.getNovel)
	mux.HandleFunc("POST /novels", a.postNovel)
	return mux
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func decodeJSON(r *http.Request, destination any) error {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("request body must contain one JSON value")
	}
	return nil
}

func readerID(r *http.Request) (string, bool) {
	value := strings.TrimSpace(r.Header.Get("X-Reader-ID"))
	return value, value != "" && len(value) <= 200
}

func pathUUID(r *http.Request, name string) (string, bool) {
	value := r.PathValue(name)
	id, err := uuid.Parse(value)
	if err != nil {
		return "", false
	}
	return id.String(), true
}

func requestedAt(r *http.Request) (*int, error) {
	value := r.URL.Query().Get("at")
	if value == "" {
		return nil, nil
	}
	at, err := strconv.Atoi(value)
	if err != nil || at < 0 {
		return nil, errors.New("at must be a nonnegative integer")
	}
	return &at, nil
}

func prepareReaderResponse(w http.ResponseWriter) {
	w.Header().Set("Cache-Control", "private, no-store")
	w.Header().Set("Vary", "X-Reader-ID")
}

func (a *API) gate(w http.ResponseWriter, r *http.Request) (string, string, int, bool) {
	requested, err := requestedAt(r)
	if err != nil {
		prepareReaderResponse(w)
		writeError(w, http.StatusBadRequest, err.Error())
		return "", "", 0, false
	}
	return a.gateAt(w, r, requested)
}

func (a *API) gateAt(w http.ResponseWriter, r *http.Request, requested *int) (string, string, int, bool) {
	prepareReaderResponse(w)
	reader, ok := readerID(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "X-Reader-ID is required")
		return "", "", 0, false
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return "", "", 0, false
	}
	progress, err := a.store.GetProgress(r.Context(), reader, novelID)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "reader progress not found")
		return "", "", 0, false
	}
	if err != nil {
		log.Printf("get progress: %v", err)
		writeError(w, http.StatusInternalServerError, "could not resolve reader progress")
		return "", "", 0, false
	}
	effective := progress.CurrentChapter
	if requested != nil && *requested < effective {
		effective = *requested
	}
	return reader, novelID, effective, true
}

func (a *API) putProgress(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	reader, ok := readerID(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "X-Reader-ID is required")
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	var request progressRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if request.Chapter == nil || *request.Chapter < 0 {
		writeError(w, http.StatusBadRequest, "chapter must be nonnegative")
		return
	}

	progress, err := a.store.AdvanceProgress(r.Context(), reader, novelID, *request.Chapter)
	switch {
	case errors.Is(err, ErrNotFound):
		writeError(w, http.StatusNotFound, "novel not found")
	case errors.Is(err, ErrChapterNotReady):
		writeError(w, http.StatusConflict, "chapter is missing or not done")
	case err != nil:
		log.Printf("advance progress: %v", err)
		writeError(w, http.StatusInternalServerError, "could not update progress")
	default:
		writeJSON(w, http.StatusOK, progress)
	}
}

func (a *API) getEntity(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	entityID, ok := pathUUID(r, "eid")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid entity id")
		return
	}
	entity, err := a.store.GetEntity(r.Context(), novelID, entityID, at)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "entity not found")
		return
	}
	if err != nil {
		log.Printf("get entity: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load entity")
		return
	}
	writeJSON(w, http.StatusOK, EntityResponse{NovelID: novelID, At: at, Entity: entity})
}

func (a *API) getWiki(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	entities, err := a.store.ListWiki(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list wiki: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load wiki")
		return
	}
	writeJSON(w, http.StatusOK, WikiResponse{NovelID: novelID, At: at, Entities: entities})
}

func (a *API) getTimeline(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	events, err := a.store.ListTimeline(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list timeline: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load timeline")
		return
	}
	writeJSON(w, http.StatusOK, TimelineResponse{NovelID: novelID, At: at, Events: events})
}

func (a *API) getRelationships(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	entityID, ok := pathUUID(r, "eid")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid entity id")
		return
	}
	relationships, err := a.store.ListRelationships(r.Context(), novelID, entityID, at)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "entity not found")
		return
	}
	if err != nil {
		log.Printf("list relationships: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load relationships")
		return
	}
	writeJSON(w, http.StatusOK, RelationshipsResponse{
		NovelID: novelID, At: at, EntityID: entityID, Relationships: relationships,
	})
}

func (a *API) getChapter(w http.ResponseWriter, r *http.Request) {
	// requested=nil: "at" has no meaning for which chapter to serve (chapter n IS the
	// resource) — gateAt is reused purely for the reader/novel validation + progress
	// lookup every other handler already does, so `progress` here is stored progress
	// with no further capping.
	_, novelID, progress, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	n, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || n < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter index")
		return
	}
	if n > progress {
		// 404, not 403 — same "don't confirm existence of gated content" posture as
		// getEntity/getRelationships.
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}

	chapter, err := a.store.GetChapter(r.Context(), novelID, n)
	switch {
	case errors.Is(err, ErrNotFound):
		writeError(w, http.StatusNotFound, "chapter not found")
	case errors.Is(err, ErrChapterNotReady):
		writeError(w, http.StatusConflict, "chapter is not ready")
	case err != nil:
		log.Printf("get chapter: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load chapter")
	default:
		writeJSON(w, http.StatusOK, ChapterResponse{
			NovelID:      novelID,
			ChapterIndex: n,
			At:           progress,
			Text:         chapter.Text,
			Spans:        chapter.Spans,
			HasNext:      chapter.HasNext,
		})
	}
}

// getNovels/getNovel are deliberately ungated — no X-Reader-ID, no gate() call. Novel
// metadata is not spoiler content (see Store.ListNovels/GetNovel's comment).
func (a *API) getNovels(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novels, err := a.store.ListNovels(r.Context())
	if err != nil {
		log.Printf("list novels: %v", err)
		writeError(w, http.StatusInternalServerError, "could not list novels")
		return
	}
	writeJSON(w, http.StatusOK, NovelListResponse{Novels: novels})
}

func (a *API) getNovel(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	novel, err := a.store.GetNovel(r.Context(), novelID)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "novel not found")
		return
	}
	if err != nil {
		log.Printf("get novel: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load novel")
		return
	}
	writeJSON(w, http.StatusOK, novel)
}

// postNovel proxies novel creation to ingest-api (the writer service) — see ingest.go.
// The request body is forwarded verbatim; reader-api does not interpret it.
func (a *API) postNovel(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.CreateNovel(r.Context(), body)
	if err != nil {
		log.Printf("create novel: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) healthz(w http.ResponseWriter, r *http.Request) {
	if err := a.store.Health(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
