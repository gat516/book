package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
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
	mux.HandleFunc("GET /queue", a.queueControl)
	mux.HandleFunc("PATCH /queue", a.queueControl)
	mux.HandleFunc("GET /healthz", a.healthz)
	mux.HandleFunc("PUT /novels/{id}/progress", a.putProgress)
	mux.HandleFunc("GET /novels/{id}/entity/{eid}", a.getEntity)
	mux.HandleFunc("GET /novels/{id}/wiki", a.getWiki)
	mux.HandleFunc("GET /novels/{id}/timeline", a.getTimeline)
	mux.HandleFunc("GET /novels/{id}/relationships/{eid}", a.getRelationships)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}", a.getChapter)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}/knowledge", a.getChapterKnowledge)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}/knowledge/activity", a.getChapterKnowledgeActivity)
	mux.HandleFunc("POST /novels/{id}/chapter/{n}/knowledge/reextract", a.chapterKnowledgeMutation)
	mux.HandleFunc("POST /novels/{id}/chapter/{n}/knowledge/reextract/{run}/apply", a.chapterKnowledgeMutation)
	mux.HandleFunc("GET /novels/{id}/chapters", a.getChapters)
	mux.HandleFunc("GET /novels/{id}/progress", a.getProgress)
	mux.HandleFunc("GET /novels/{id}/knowledge-status", a.getKnowledgeStatus)
	mux.HandleFunc("GET /novels/{id}/event-status", a.getEventStatus)
	mux.HandleFunc("GET /novels/{id}/pipeline", a.getPipelineStatus)
	mux.HandleFunc("POST /novels/{id}/translate-ahead", a.postTranslateAhead)
	mux.HandleFunc("PATCH /novels/{id}/settings", a.patchNovelSettings)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}/preview", a.getChapterPreview)
	mux.HandleFunc("GET /novels/{id}/translation-health", a.getTranslationHealth)
	mux.HandleFunc("GET /novels/{id}/repair", a.getRepairStatus)
	mux.HandleFunc("GET /novels/{id}/repair/preview", a.getRepairPreview)
	mux.HandleFunc("GET /novels/{id}/repair/progress", a.getRepairProgress)
	mux.HandleFunc("POST /novels/{id}/repair", a.postRepair)
	mux.HandleFunc("DELETE /novels/{id}/repair/{request}", a.deleteRepair)
	mux.HandleFunc("PATCH /novels/{id}/facts/{fact}/display", a.mutateFact)
	mux.HandleFunc("POST /novels/{id}/facts/{fact}/corrections", a.mutateFact)
	mux.HandleFunc("DELETE /novels/{id}/facts/{fact}", a.mutateFact)
	mux.HandleFunc("POST /novels/{id}/ask", a.postAsk)
	mux.HandleFunc("GET /novels", a.getNovels)
	mux.HandleFunc("GET /novels/{id}", a.getNovel)
	mux.HandleFunc("POST /novels", a.postNovel)
	mux.HandleFunc("DELETE /novels/{id}", a.deleteNovel)
	mux.HandleFunc("DELETE /novels/{id}/graph", a.deleteGraph)
	mux.HandleFunc("POST /novels/{id}/chapters", a.postChapter)
	mux.HandleFunc("POST /novels/{id}/scrape", a.postScrape)
	mux.HandleFunc("GET /novels/{id}/scrape/status", a.getScrapeStatus)
	mux.HandleFunc("POST /novels/{id}/scrape/cancel", a.postScrapeCancel)
	mux.HandleFunc("GET /novels/{id}/glossary", a.getGlossary)
	mux.HandleFunc("PATCH /novels/{id}/glossary/{term}", a.patchGlossaryTerm)
	mux.HandleFunc("DELETE /novels/{id}/glossary/{term}", a.patchGlossaryTerm)
	mux.HandleFunc("POST /novels/{id}/glossary/bootstrap", a.postBootstrapGlossary)
	mux.HandleFunc("POST /novels/{id}/glossary/confirm", a.postConfirmGlossaryTerm)
	mux.HandleFunc("GET /novels/{id}/name-reviews", a.getCharacterNameReviews)
	mux.HandleFunc("POST /novels/{id}/name-reviews/{term}/approve", a.approveCharacterName)
	mux.HandleFunc("GET /novels/{id}/provider-config", a.getProviderConfig)
	mux.HandleFunc("GET /novels/{id}/provider-config/ollama-models", a.getOllamaModels)
	mux.HandleFunc("GET /provider-credentials", a.listProviderCredentials)
	mux.HandleFunc("PUT /provider-credentials/{provider}", a.putProviderCredential)
	mux.HandleFunc("DELETE /provider-credentials/{provider}", a.deleteProviderCredential)
	mux.HandleFunc("PATCH /novels/{id}/provider-config", a.putProviderConfig)
	return mux
}

func (a *API) queueControl(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 4096))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read queue settings")
		return
	}
	if r.Method == http.MethodPatch {
		// The UI never chooses the actor itself. This is an audit label for a shared
		// local reader, not a substitute for administrator authentication.
		var patch map[string]json.RawMessage
		if err := json.Unmarshal(body, &patch); err != nil {
			writeError(w, http.StatusBadRequest, "invalid queue settings")
			return
		}
		actor := "unknown reader"
		if id, ok := readerID(r); ok {
			actor = id
		}
		encoded, _ := json.Marshal(actor)
		patch["changed_by"] = encoded
		body, _ = json.Marshal(patch)
	}
	result, status, err := a.ingest.QueueControl(r.Context(), r.Method, body)
	if err != nil {
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) getCharacterNameReviews(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	if status := r.URL.Query().Get("status"); status != "" && status != "pending" {
		writeError(w, http.StatusBadRequest, "only status=pending is supported")
		return
	}
	var chapter *int
	if raw := r.URL.Query().Get("chapter"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 {
			writeError(w, http.StatusBadRequest, "chapter must be a nonnegative integer")
			return
		}
		chapter = &value
	}
	reviews, err := a.store.ListNameReviews(r.Context(), novelID, chapter)
	if err != nil {
		log.Printf("list name reviews: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load name reviews")
		return
	}
	writeJSON(w, http.StatusOK, CharacterNameReviewsResponse{NovelID: novelID, Reviews: reviews})
}

func (a *API) approveCharacterName(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	reviewer, ok := readerID(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "X-Reader-ID is required")
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	var value map[string]any
	if err := json.Unmarshal(body, &value); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	value["reviewer"] = reviewer
	body, _ = json.Marshal(value)
	result, status, err := a.ingest.ApproveCharacterName(r.Context(), novelID, r.PathValue("term"), body)
	if err != nil {
		log.Printf("approve character name: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
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
		// Reading pulls translation forward: keep the novel's lookahead window queued
		// ahead of wherever the reader just reached (migration 0015). Best-effort — a
		// failure here means the next chapter isn't queued yet, not that the reader's
		// progress failed, so it must never turn a successful advance into an error.
		a.queueAhead(r.Context(), novelID, progress.CurrentChapter+1)
		writeJSON(w, http.StatusOK, progress)
	}
}

// queueAhead asks ingest-api to queue the novel's lookahead window starting at `from`.
// Count is omitted so ingest-api applies novel.translate_lookahead. Errors are logged and
// swallowed: every caller is doing something else that already succeeded.
func (a *API) queueAhead(ctx context.Context, novelID string, from int) {
	// reader-api can run without an ingest client wired (its read paths don't need one),
	// and advancing progress must not panic when it isn't there — queueing ahead is an
	// optimisation, never a requirement for the reader to make progress.
	if a.ingest == nil {
		return
	}
	body, err := json.Marshal(map[string]int{"from": from})
	if err != nil {
		return
	}
	if _, _, err := a.ingest.TranslateAhead(ctx, novelID, body); err != nil {
		log.Printf("queue ahead for %s from %d: %v", novelID, from, err)
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
	writeJSON(w, http.StatusOK, EntityResponse{Knowledge: entity.Knowledge, NovelID: novelID, At: at, Entity: entity})
}

func (a *API) getWiki(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	knowledge, knowledgeErr := a.store.KnowledgeStatus(r.Context(), novelID, at, at)
	if knowledgeErr != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
		return
	}
	entities, err := a.store.ListWiki(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list wiki: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load wiki")
		return
	}
	if !a.knowledgeUnchanged(w, r, novelID, at, knowledge) {
		return
	}
	writeJSON(w, http.StatusOK, WikiResponse{Knowledge: knowledge, NovelID: novelID, At: at, Entities: entities})
}

func (a *API) getTimeline(w http.ResponseWriter, r *http.Request) {
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	knowledge, knowledgeErr := a.store.KnowledgeStatus(r.Context(), novelID, at, at)
	if knowledgeErr != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
		return
	}
	eventKnowledge, eventKnowledgeErr := a.store.EventStatus(r.Context(), novelID, at, at)
	if eventKnowledgeErr != nil {
		writeError(w, http.StatusInternalServerError, "could not load event status")
		return
	}
	events, err := a.store.ListTimeline(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list timeline: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load timeline")
		return
	}
	if !a.knowledgeUnchanged(w, r, novelID, at, knowledge) {
		return
	}
	eventAfter, err := a.store.EventStatus(r.Context(), novelID, at, at)
	if err != nil || eventAfter.RevisionID != eventKnowledge.RevisionID || eventAfter.Version != eventKnowledge.Version {
		writeError(w, http.StatusConflict, "events changed; retry request")
		return
	}
	writeJSON(w, http.StatusOK, TimelineResponse{Knowledge: knowledge, EventKnowledge: eventKnowledge, NovelID: novelID, At: at, Events: events})
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
	knowledge, knowledgeErr := a.store.KnowledgeStatus(r.Context(), novelID, at, at)
	if knowledgeErr != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
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
	if !a.knowledgeUnchanged(w, r, novelID, at, knowledge) {
		return
	}
	writeJSON(w, http.StatusOK, RelationshipsResponse{Knowledge: knowledge,
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

	chapter, err := a.store.GetChapter(r.Context(), novelID, n, progress)
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
			Knowledge:          chapter.Knowledge,
			EventKnowledge:     chapter.EventKnowledge,
			NovelID:            novelID,
			ChapterIndex:       n,
			At:                 progress,
			Text:               chapter.Text,
			Spans:              chapter.Spans,
			NewFacts:           chapter.NewFacts,
			Events:             chapter.Events,
			HasNext:            chapter.HasNext,
			SiteChapterNo:      chapter.SiteChapterNo,
			SourceURL:          chapter.SourceURL,
			Part:               chapter.Part,
			TranslationWarning: chapter.TranslationWarning,
		})
	}
}

// defaultChapterPage/maxChapterPage bound the chapter index: a scraped novel can hold
// thousands of chapters, so this endpoint is never unbounded.
const (
	defaultChapterPage = 100
	maxChapterPage     = 500
)

// getChapters serves the paged chapter index (metadata only — see store.ListChapters for
// why it is deliberately ungated). Progress is reported best-effort: a reader with no
// progress row yet gets 0 rather than a 404, since the index is exactly what a brand-new
// reader needs in order to pick a starting chapter.
func (a *API) getChapters(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}

	limit := defaultChapterPage
	if raw := r.URL.Query().Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 {
			writeError(w, http.StatusBadRequest, "invalid limit")
			return
		}
		limit = min(parsed, maxChapterPage)
	}
	offset := 0
	if raw := r.URL.Query().Get("offset"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 0 {
			writeError(w, http.StatusBadRequest, "invalid offset")
			return
		}
		offset = parsed
	}

	progress := 0
	if reader, ok := readerID(r); ok {
		if stored, err := a.store.GetProgress(r.Context(), reader, novelID); err == nil {
			progress = stored.CurrentChapter
		} else if !errors.Is(err, ErrNotFound) {
			log.Printf("get progress for chapter list: %v", err)
		}
	}

	chapters, total, err := a.store.ListChapters(r.Context(), novelID, limit, offset)
	if err != nil {
		log.Printf("list chapters: %v", err)
		writeError(w, http.StatusInternalServerError, "could not list chapters")
		return
	}
	writeJSON(w, http.StatusOK, ChapterListResponse{
		NovelID:  novelID,
		Chapters: chapters,
		Total:    total,
		Limit:    limit,
		Offset:   offset,
		Progress: progress,
	})
}

// getChapterPreview serves the partial translation of a chapter still being translated, so
// a reader waiting on it watches the text arrive instead of a spinner. Returns
// available:false (not 404) when nothing is streaming — "no preview yet" is the ordinary
// state, not an error, and the client polls the same endpoint either way.
func (a *API) getChapterPreview(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	n, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || n < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter index")
		return
	}
	text, available, status, err := a.store.TranslationPreview(r.Context(), novelID, n)
	if err != nil {
		log.Printf("translation preview: %v", err)
		writeError(w, http.StatusInternalServerError, "could not read preview")
		return
	}
	writeJSON(w, http.StatusOK, ChapterPreviewResponse{
		NovelID:      novelID,
		ChapterIndex: n,
		Available:    available,
		Text:         text,
		Status:       status,
	})
}

// patchNovelSettings proxies a work-window change to ingest-api, which owns novel writes.
// Ungated like the other novel-administration routes: it tunes how much work the system
// does for a novel, and exposes no chapter content.
func (a *API) patchNovelSettings(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.UpdateNovelSettings(r.Context(), novelID, body)
	if err != nil {
		log.Printf("update novel settings: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// getTranslationHealth reports whether this novel's terminology is being translated
// consistently. Ungated: it returns counts and a judgement, never chapter content.
func (a *API) getTranslationHealth(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	health, err := a.store.TranslationHealth(r.Context(), novelID)
	if err != nil {
		log.Printf("translation health: %v", err)
		writeError(w, http.StatusInternalServerError, "could not read translation health")
		return
	}
	writeJSON(w, http.StatusOK, health)
}

// postTranslateAhead proxies a translation-window request to ingest-api, which owns
// chapter writes. Ungated: it queues work, it doesn't reveal chapter content, and the
// chapters it queues remain unreadable until the reader's own progress reaches them.
func (a *API) postTranslateAhead(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.TranslateAhead(r.Context(), novelID, body)
	if err != nil {
		log.Printf("translate ahead: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// getPipelineStatus reports what the worker is doing right now for this novel. Ungated
// and reader-agnostic (no X-Reader-ID): it exposes queue mechanics — a chapter index, a
// stage name, an elapsed time — and no chapter content whatsoever, so there is nothing
// here for the spoiler gate to protect.
func (a *API) getPipelineStatus(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	status, err := a.store.PipelineStatus(r.Context(), novelID)
	if err != nil {
		log.Printf("pipeline status: %v", err)
		writeError(w, http.StatusInternalServerError, "could not read pipeline status")
		return
	}
	writeJSON(w, http.StatusOK, status)
}

// getProgress reports where this reader left off, so the UI can reopen the novel on the
// chapter they were last reading instead of restarting at chapter 1. The PUT counterpart
// already existed; this read side did not.
func (a *API) getProgress(w http.ResponseWriter, r *http.Request) {
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
	progress, err := a.store.GetProgress(r.Context(), reader, novelID)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "reader progress not found")
		return
	}
	if err != nil {
		log.Printf("get progress: %v", err)
		writeError(w, http.StatusInternalServerError, "could not resolve reader progress")
		return
	}
	writeJSON(w, http.StatusOK, progress)
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

// deleteNovel proxies novel deletion to ingest-api, which owns the cascade (migration
// 0030). Irreversible: it removes the novel's chapters, graph, glossary and queued work.
func (a *API) deleteNovel(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	result, status, err := a.ingest.DeleteNovel(r.Context(), novelID)
	if err != nil {
		log.Printf("delete novel: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// deleteGraph is temporarily reader-facing until accounts own their own graphs. The
// writer service still owns the destructive transaction; this API never gets broad
// database write privileges. It returns no story data, so the spoiler gate is unchanged.
func (a *API) deleteGraph(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	result, status, err := a.ingest.DeleteGraph(r.Context(), novelID)
	if err != nil {
		log.Printf("delete graph: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// postChapter proxies chapter paste to ingest-api — see ingest.go. Unauthenticated on
// ingest-api's side by design (only POST /novels is token-gated there); this route
// exists so the browser only ever talks to reader-api, per vite.config.ts's invariant.
func (a *API) postChapter(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.PasteChapter(r.Context(), novelID, body)
	if err != nil {
		log.Printf("paste chapter: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

type scrapeRequest struct {
	StartURL string `json:"start_url"`
	Mode     string `json:"mode"` // "translate" (default) | "bootstrap"
}

// postScrape starts a scrape job (PLAN.md Phase N5) — ungated like postNovel, since
// there's no reader-identity concept for "start ingesting a novel," same as creation.
// Which sites/URLs are actually supported is the scraper service's concern, not
// reader-api's; an unsupported host surfaces as the job's own status=error, visible via
// the status endpoint, rather than being validated twice in two services.
func (a *API) postScrape(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	var req scrapeRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	parsed, err := url.Parse(req.StartURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		writeError(w, http.StatusBadRequest, "start_url must be an absolute http(s) URL")
		return
	}
	if req.Mode == "" {
		req.Mode = "translate"
	}
	if req.Mode != "translate" && req.Mode != "bootstrap" {
		writeError(w, http.StatusBadRequest, `mode must be "translate" or "bootstrap"`)
		return
	}

	id, err := a.store.CreateScrapeJob(r.Context(), novelID, req.StartURL, req.Mode)
	switch {
	case errors.Is(err, ErrScrapeJobActive):
		writeError(w, http.StatusConflict, "a scrape is already running for this novel")
	case err != nil:
		log.Printf("create scrape job: %v", err)
		writeError(w, http.StatusInternalServerError, "could not start scrape")
	default:
		writeJSON(w, http.StatusAccepted, map[string]int64{"id": id})
	}
}

func (a *API) getScrapeStatus(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	job, err := a.store.LatestScrapeJob(r.Context(), novelID)
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "no scrape job for this novel")
		return
	}
	if err != nil {
		log.Printf("get scrape status: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load scrape status")
		return
	}
	writeJSON(w, http.StatusOK, job)
}

func (a *API) postScrapeCancel(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	if err := a.store.RequestScrapeCancel(r.Context(), novelID); err != nil {
		log.Printf("cancel scrape: %v", err)
		writeError(w, http.StatusInternalServerError, "could not request cancellation")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "cancel_requested"})
}

func (a *API) getGlossary(w http.ResponseWriter, r *http.Request) {
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
	requested, err := requestedAt(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	progress, err := a.store.GetProgress(r.Context(), reader, novelID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusInternalServerError, "could not resolve reader progress")
		return
	}
	// Before the first readable chapter, expose only manually seeded terms (chapter 0).
	at := 0
	if err == nil {
		at = progress.CurrentChapter
	}
	if requested != nil {
		at = min(at, *requested)
	}
	knowledge, knowledgeErr := a.store.KnowledgeStatus(r.Context(), novelID, at, at)
	if knowledgeErr != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
		return
	}
	terms, err := a.store.ListGlossary(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list glossary: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load glossary")
		return
	}
	if !a.knowledgeUnchanged(w, r, novelID, at, knowledge) {
		return
	}
	writeJSON(w, http.StatusOK, GlossaryResponse{Knowledge: knowledge, NovelID: novelID, At: at, Terms: terms})
}

// patchGlossaryTerm proxies a human correction to ingest-api (see ingest.go). Gated with
// X-Reader-ID like every other reader-api write (the accepted repo-wide "fake principal"
// tradeoff) — unlike postNovel/postChapter/postScrape, which have no reader-identity
// concept at all, a glossary correction is an action a specific reader takes.
func (a *API) patchGlossaryTerm(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if _, ok := readerID(r); !ok {
		writeError(w, http.StatusUnauthorized, "X-Reader-ID is required")
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	var result json.RawMessage
	var status int
	if r.Method == http.MethodDelete {
		result, status, err = a.ingest.DeleteGlossaryTerm(r.Context(), novelID, r.PathValue("term"), body)
	} else {
		result, status, err = a.ingest.CorrectGlossaryTerm(r.Context(), novelID, r.PathValue("term"), body)
	}
	if err != nil {
		log.Printf("correct glossary term: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// postBootstrapGlossary proxies a glossary bootstrap (PLAN.md Phase N6) to ingest-api.
// No X-Reader-ID gate, matching postNovel/postChapter/postScrape: this is novel setup,
// not an action tied to a specific reader's progress.
func (a *API) postBootstrapGlossary(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.BootstrapGlossary(r.Context(), novelID, body)
	if err != nil {
		log.Printf("bootstrap glossary: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) postConfirmGlossaryTerm(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	if _, ok := readerID(r); !ok {
		writeError(w, http.StatusUnauthorized, "X-Reader-ID is required")
		return
	}
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.ConfirmGlossaryTerm(r.Context(), novelID, body)
	if err != nil {
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// getProviderConfig proxies a novel's masked provider config from ingest-api. No
// X-Reader-ID gate (mirrors postNovel/postScrape — this is novel administration, not a
// reader-identity-scoped action).
func (a *API) getProviderConfig(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	result, status, err := a.ingest.GetProviderConfig(r.Context(), novelID)
	if err != nil {
		log.Printf("get provider config: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) getOllamaModels(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	result, status, err := a.ingest.ListOllamaModels(
		r.Context(), novelID, r.URL.Query().Get("target") == "graph",
	)
	if err != nil {
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// listProviderCredentials proxies the masked list of global provider credentials.
// Ungated like the other administration routes: no chapter content, and the response
// reports api_key_set rather than any key.
func (a *API) listProviderCredentials(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	result, status, err := a.ingest.ListProviderCredentials(r.Context())
	if err != nil {
		log.Printf("list provider credentials: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// putProviderCredential proxies a global credential create/replace to ingest-api.
func (a *API) putProviderCredential(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.PutProviderCredential(r.Context(), r.PathValue("provider"), body)
	if err != nil {
		log.Printf("put provider credential: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// deleteProviderCredential proxies removal of a global credential. This is the only way
// to clear a stored key -- an omitted key on PUT means "unchanged", never "erase".
func (a *API) deleteProviderCredential(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	result, status, err := a.ingest.DeleteProviderCredential(r.Context(), r.PathValue("provider"))
	if err != nil {
		log.Printf("delete provider credential: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	if status == http.StatusNoContent {
		w.WriteHeader(status)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

// putProviderConfig proxies a provider-config create/replace to ingest-api.
func (a *API) putProviderConfig(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.PutProviderConfig(r.Context(), novelID, body)
	if err != nil {
		log.Printf("put provider config: %v", err)
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
