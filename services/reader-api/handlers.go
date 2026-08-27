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
	mux.HandleFunc("GET /healthz", a.healthz)
	mux.HandleFunc("PUT /novels/{id}/progress", a.putProgress)
	mux.HandleFunc("GET /novels/{id}/entity/{eid}", a.getEntity)
	mux.HandleFunc("GET /novels/{id}/wiki", a.getWiki)
	mux.HandleFunc("GET /novels/{id}/timeline", a.getTimeline)
	mux.HandleFunc("GET /novels/{id}/relationships/{eid}", a.getRelationships)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}", a.getChapter)
	mux.HandleFunc("GET /novels/{id}/chapters", a.getChapters)
	mux.HandleFunc("GET /novels/{id}/progress", a.getProgress)
	mux.HandleFunc("GET /novels/{id}/pipeline", a.getPipelineStatus)
	mux.HandleFunc("POST /novels/{id}/translate-ahead", a.postTranslateAhead)
	mux.HandleFunc("GET /novels/{id}/chapter/{n}/preview", a.getChapterPreview)
	mux.HandleFunc("POST /novels/{id}/ask", a.postAsk)
	mux.HandleFunc("GET /novels", a.getNovels)
	mux.HandleFunc("GET /novels/{id}", a.getNovel)
	mux.HandleFunc("POST /novels", a.postNovel)
	mux.HandleFunc("POST /novels/{id}/chapters", a.postChapter)
	mux.HandleFunc("POST /novels/{id}/scrape", a.postScrape)
	mux.HandleFunc("GET /novels/{id}/scrape/status", a.getScrapeStatus)
	mux.HandleFunc("POST /novels/{id}/scrape/cancel", a.postScrapeCancel)
	mux.HandleFunc("GET /novels/{id}/glossary", a.getGlossary)
	mux.HandleFunc("PATCH /novels/{id}/glossary/{term}", a.patchGlossaryTerm)
	mux.HandleFunc("POST /novels/{id}/glossary/bootstrap", a.postBootstrapGlossary)
	mux.HandleFunc("GET /novels/{id}/provider-config", a.getProviderConfig)
	mux.HandleFunc("PATCH /novels/{id}/provider-config", a.putProviderConfig)
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
			NovelID:       novelID,
			ChapterIndex:  n,
			At:            progress,
			Text:          chapter.Text,
			Spans:         chapter.Spans,
			HasNext:       chapter.HasNext,
			SiteChapterNo: chapter.SiteChapterNo,
			Part:          chapter.Part,
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
	_, novelID, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	terms, err := a.store.ListGlossary(r.Context(), novelID, at)
	if err != nil {
		log.Printf("list glossary: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load glossary")
		return
	}
	writeJSON(w, http.StatusOK, GlossaryResponse{NovelID: novelID, At: at, Terms: terms})
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
	result, status, err := a.ingest.CorrectGlossaryTerm(r.Context(), novelID, r.PathValue("term"), body)
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
