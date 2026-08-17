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
	store ReaderStore
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
	requested, err := requestedAt(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
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

func (a *API) healthz(w http.ResponseWriter, r *http.Request) {
	if err := a.store.Health(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
