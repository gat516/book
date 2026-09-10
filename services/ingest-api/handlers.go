package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

// API holds the dependencies the HTTP handlers need.
type API struct {
	store *Store
	cfg   Config

	providerHealthMu    sync.Mutex
	providerHealthCache map[string]providerHealthCacheEntry
}

// --- request/response bodies ---

type createNovelReq struct {
	Title      string `json:"title"`
	SourceLang string `json:"source_lang"`
	TargetLang string `json:"target_lang"`
	Genre      string `json:"genre"` // optional; selects a preset ontology (§4.1)

	// ProviderConfig is optional (PLAN.md Phase N3): a novel's own LLM provider/model/API
	// key, encrypted at rest, overriding LLM_PROVIDER for this novel only. Omit entirely
	// to use the process-wide default.
	ProviderConfig *providerConfigReq `json:"provider_config,omitempty"`

	// The two work windows, both optional (nil keeps the schema default). Separate because
	// the work behind them differs by orders of magnitude — fetching is one HTTP request,
	// translating is a dozen-plus sequential LLM calls — so a novel usually wants a wide
	// ingest window over a narrow translate one. 0 means unlimited.
	IngestLookahead    *int `json:"ingest_lookahead,omitempty"`    // chapters to FETCH ahead (0018)
	TranslateLookahead *int `json:"translate_lookahead,omitempty"` // chapters to TRANSLATE ahead (0015)
}

// novelSettingsReq changes the work windows after creation. Both optional; omitted fields
// are left as they are rather than reset.
type novelSettingsReq struct {
	IngestLookahead    *int `json:"ingest_lookahead,omitempty"`
	TranslateLookahead *int `json:"translate_lookahead,omitempty"`
}

type providerConfigReq struct {
	Provider       string `json:"provider"` // anthropic|deepseek|gemini|groq|ollama
	Model          string `json:"model,omitempty"`
	TranslateModel string `json:"translate_model,omitempty"`
	ExtractModel   string `json:"extract_model,omitempty"`
	BaseURL        string `json:"base_url,omitempty"`
}

type createNovelResp struct {
	ID string `json:"id"`
}

type pasteChapterReq struct {
	ChapterIndex int    `json:"chapter_index"`
	RawText      string `json:"raw_text"`
	// TranslatedText is optional (PLAN.md N5/N6 half-translated bootstrap): when set,
	// chapter.translated_uri is populated at insert time so TranslateStage skips the LLM
	// call for this chapter entirely. SiteChapterNo is the site's own printed chapter
	// label (metadata only, NEVER the gate key — instructions.md §3.1) — set by the
	// scraper, or by a human who knows the source site's numbering.
	TranslatedText string `json:"translated_text,omitempty"`
	SiteChapterNo  string `json:"site_chapter_no,omitempty"`
	// SourceURL is durable provenance and a reader escape hatch when automation is down.
	// It is metadata only, never fetched by ingest-api or used as a gate key.
	SourceURL string `json:"source_url,omitempty"`
	// Part is which piece of a multi-page source chapter this is (see SourceMeta.Part).
	// Omitted or 0 means part 1 — an ordinary chapter is "part 1" without the caller
	// having to say so.
	Part int `json:"part,omitempty"`
	// Enqueue controls whether accepting this chapter also queues it for translation.
	// Absent means true, so a human pasting one chapter still gets it translated
	// immediately. The scraper sets it false: it ingests far faster than the pipeline can
	// translate, and queueing every fetched chapter is what buried the reader's own
	// chapters behind hundreds nobody was reading. reader-api queues those on demand
	// instead, near the reader's position (novel.translate_lookahead, migration 0015).
	Enqueue *bool `json:"enqueue,omitempty"`
}

type pasteChapterResp struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
	RawHash      string `json:"raw_hash"`
	Status       string `json:"status"`
	// Duplicate reports that this exact body was already ingested for this novel, at the
	// ChapterIndex returned above (which is the EXISTING chapter's index, not the one the
	// caller asked for). Nothing was written. Callers that walk a source — the scraper —
	// use this to skip past already-ingested pages instead of re-adding them.
	Duplicate bool `json:"duplicate,omitempty"`
}

// writeJSON marshals v and writes it with the given status. Small helper so handlers stay
// focused on logic rather than plumbing.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// createNovel handles POST /novels. It resolves an ontology from the genre preset and
// inserts the novel — a prerequisite for pasting chapters (chapter FKs novel).
func (a *API) createNovel(w http.ResponseWriter, r *http.Request) {
	var req createNovelReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.Title == "" {
		writeErr(w, http.StatusBadRequest, "title is required")
		return
	}
	// Language defaults keep the common same-language case a one-field request; when
	// source == target the translate stage is skipped entirely downstream (§0.5).
	if req.SourceLang == "" {
		req.SourceLang = "en"
	}
	if req.TargetLang == "" {
		req.TargetLang = req.SourceLang
	}

	var providerConfig *ProviderConfigInput
	if req.ProviderConfig != nil {
		pc, err := a.buildProviderConfigInput(*req.ProviderConfig)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		providerConfig = &pc
	}

	ont := ontologyForGenre(req.Genre)
	id, err := a.store.insertNovel(r.Context(), req.Title, req.SourceLang, req.TargetLang, req.Genre, ont,
		providerConfig, req.IngestLookahead, req.TranslateLookahead)
	if err != nil {
		log.Printf("createNovel: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not create novel")
		return
	}
	writeJSON(w, http.StatusCreated, createNovelResp{ID: id})
}

// buildProviderConfigInput validates req. It handles no secret: a novel names a provider,
// and the key for that provider comes from provider_credential (migration 0080).
func (a *API) buildProviderConfigInput(req providerConfigReq) (ProviderConfigInput, error) {
	switch req.Provider {
	case "anthropic", "deepseek", "gemini", "groq", "ollama":
	default:
		return ProviderConfigInput{}, fmt.Errorf("provider must be one of anthropic, deepseek, gemini, groq, ollama")
	}
	if req.Provider == "ollama" && req.BaseURL != "" {
		if err := validateOllamaBaseURL(req.BaseURL, a.cfg.OllamaAllowedHosts); err != nil {
			return ProviderConfigInput{}, err
		}
	}

	// model is the legacy one-model field. Retain it for old clients, but when the new
	// stage fields are omitted it deliberately configures both paths identically.
	if req.TranslateModel == "" {
		req.TranslateModel = req.Model
	}
	if req.ExtractModel == "" {
		req.ExtractModel = req.Model
	}
	return ProviderConfigInput{Provider: req.Provider, Model: req.Model, TranslateModel: req.TranslateModel, ExtractModel: req.ExtractModel, BaseURL: req.BaseURL}, nil
}

// getProviderConfig handles GET /novels/{id}/provider-config.
func (a *API) getProviderConfig(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")
	view, err := a.store.GetProviderConfig(r.Context(), novelID)
	if errors.Is(err, ErrProviderConfigNotFound) {
		writeErr(w, http.StatusNotFound, "no provider config for this novel")
		return
	}
	if err != nil {
		log.Printf("getProviderConfig: %v", err)
		writeErr(w, http.StatusInternalServerError, "lookup failed")
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// putProviderConfig handles PATCH /novels/{id}/provider-config — replaces the novel's
// provider config wholesale.
func (a *API) putProviderConfig(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")
	var req providerConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	in, err := a.buildProviderConfigInput(req)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := a.store.UpsertProviderConfig(r.Context(), novelID, in); err != nil {
		log.Printf("putProviderConfig: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not save provider config")
		return
	}
	view, err := a.store.GetProviderConfig(r.Context(), novelID)
	if err != nil {
		log.Printf("putProviderConfig readback: %v", err)
		writeErr(w, http.StatusInternalServerError, "saved but readback failed")
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// pasteChapter handles POST /novels/{id}/chapters — the core paste-ingest path (§7.1):
// hash → object store → chapter row → enqueue pointer. Idempotent end to end.
func (a *API) pasteChapter(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")

	var req pasteChapterReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.RawText == "" {
		writeErr(w, http.StatusBadRequest, "raw_text is required")
		return
	}
	if req.ChapterIndex < 0 {
		writeErr(w, http.StatusBadRequest, "chapter_index must be >= 0")
		return
	}
	if req.SourceURL != "" {
		parsed, err := url.Parse(req.SourceURL)
		if err != nil || len(req.SourceURL) > 4096 || parsed.Host == "" ||
			(parsed.Scheme != "http" && parsed.Scheme != "https") {
			writeErr(w, http.StatusBadRequest, "source_url must be an absolute http(s) URL")
			return
		}
	}

	// Look up the novel's source language (also validates the novel exists).
	sourceLang, err := a.store.getNovelSourceLang(r.Context(), novelID)
	if errors.Is(err, pgx.ErrNoRows) {
		writeErr(w, http.StatusNotFound, "novel not found")
		return
	} else if err != nil {
		log.Printf("pasteChapter lookup: %v", err)
		writeErr(w, http.StatusInternalServerError, "lookup failed")
		return
	}

	// A page URL is the cheapest scrape identity. On a re-scrape this returns before any
	// object write, queue pointer, or LLM-capable worker can see the page. Same-index
	// re-pastes intentionally continue, preserving the existing retry affordance.
	if req.SourceURL != "" {
		existingIndex, existingHash, found, err := a.store.chapterBySourceURL(r.Context(), novelID, req.SourceURL)
		if err != nil {
			log.Printf("pasteChapter source URL dedup lookup: %v", err)
			writeErr(w, http.StatusInternalServerError, "lookup failed")
			return
		}
		if found && existingIndex != req.ChapterIndex {
			writeJSON(w, http.StatusOK, pasteChapterResp{
				NovelID: novelID, ChapterIndex: existingIndex, RawHash: existingHash,
				Status: "duplicate", Duplicate: true,
			})
			return
		}
	}

	// raw_hash is the second, content-addressed identity check (§3.1). It catches the
	// same chapter published at a changed URL or a mirror site.
	sum := sha256.Sum256([]byte(req.RawText))
	rawHash := "sha256:" + hex.EncodeToString(sum[:])

	// Content-addressed dedup (migration 0013). A re-run scrape re-walks pages already
	// ingested and offers them under FRESH chapter indices (the scraper assigns MAX+1 at
	// job start), so nothing conflicts on (novel_id, chapter_index) and every chapter gets
	// silently duplicated — observed for real, 27 duplicate chapters in one novel.
	//
	// Only a hash match at a DIFFERENT index is a duplicate. A match at the SAME index is
	// an ordinary re-paste, which stays deliberately allowed: it falls through to the
	// enqueue below so a stalled pipeline can be retriggered by re-pasting (see that call's
	// comment). Collapsing both cases here would quietly remove that affordance.
	existingIndex, found, err := a.store.chapterIndexByHash(r.Context(), novelID, rawHash)
	if err != nil {
		log.Printf("pasteChapter dedup lookup: %v", err)
		writeErr(w, http.StatusInternalServerError, "lookup failed")
		return
	}
	if found && existingIndex != req.ChapterIndex {
		// A re-scrape can teach a legacy row its original page URL even though content-hash
		// dedup correctly refuses to insert the chapter again.
		if err := a.store.recordChapterSourceURL(r.Context(), novelID, existingIndex, req.SourceURL); err != nil {
			log.Printf("pasteChapter record duplicate source URL: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not record chapter source")
			return
		}
		writeJSON(w, http.StatusOK, pasteChapterResp{
			NovelID:      novelID,
			ChapterIndex: existingIndex,
			RawHash:      rawHash,
			Status:       "duplicate",
			Duplicate:    true,
		})
		return
	}

	// Big body → object store; keep only a pointer in Postgres.
	rawURI, err := a.store.putRawObject(r.Context(), novelID, req.ChapterIndex, req.RawText)
	if err != nil {
		log.Printf("pasteChapter putObject: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not store chapter body")
		return
	}

	// Pre-translated text (PLAN.md N5/N6): store it too and point translated_uri at it
	// immediately, so TranslateStage's early-out (services/pipeline/pipeline/stages/
	// translate.py) skips the LLM call for this chapter entirely. translated_by records
	// that the translation is external, not model-served, since no provider:model pin
	// applies here.
	var translatedURI, translatedBy string
	if req.TranslatedText != "" {
		translatedURI, err = a.store.putTranslatedObject(r.Context(), novelID, req.ChapterIndex, req.TranslatedText)
		if err != nil {
			log.Printf("pasteChapter putTranslatedObject: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not store translated body")
			return
		}
		translatedBy = "external"
	}

	// "Part 1 unless told otherwise": normalize here rather than storing 0, so every
	// consumer reads a real 1-based part without repeating this defaulting.
	part := req.Part
	if part < 1 {
		part = 1
	}

	env := ChapterEnvelope{
		NovelID:      novelID,
		ChapterIndex: req.ChapterIndex,
		RawText:      req.RawText,
		SourceLang:   sourceLang,
		SourceMeta: SourceMeta{
			SourceURL:     req.SourceURL,
			FetchedAt:     time.Now().UTC().Format(time.RFC3339),
			RawHash:       rawHash,
			Adapter:       "paste",
			SiteChapterNo: req.SiteChapterNo,
			Part:          part,
		},
	}

	if err := a.store.insertChapter(r.Context(), env, rawURI, translatedURI, translatedBy); err != nil {
		log.Printf("pasteChapter insert: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not record chapter")
		return
	}
	// Also enrich a same-index re-paste whose original row predates source_url support;
	// insertChapter itself remains a content-idempotent ON CONFLICT no-op.
	if err := a.store.recordChapterSourceURL(r.Context(), novelID, req.ChapterIndex, req.SourceURL); err != nil {
		log.Printf("pasteChapter record source URL: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not record chapter source")
		return
	}

	// Signal the pipeline, unless the caller is ingesting ahead of the reader. We enqueue
	// even on re-paste so a stalled pipeline can be retriggered; the pipeline itself dedups
	// on content (§6.1).
	if req.Enqueue == nil || *req.Enqueue {
		if err := a.store.enqueue(r.Context(), QueueMessage{NovelID: novelID, ChapterIndex: req.ChapterIndex}); err != nil {
			log.Printf("pasteChapter enqueue: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not enqueue job")
			return
		}
	}

	writeJSON(w, http.StatusAccepted, pasteChapterResp{
		NovelID:      novelID,
		ChapterIndex: req.ChapterIndex,
		RawHash:      rawHash,
		Status:       "ingested",
	})
}

// patchNovelSettings changes a novel's work windows after creation, so the ingest and
// translate depths can be tuned against how a particular book actually behaves rather than
// being fixed at creation.
func (a *API) patchNovelSettings(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")

	var req novelSettingsReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	// Negative is meaningless for a window; 0 legitimately means "unlimited".
	if (req.IngestLookahead != nil && *req.IngestLookahead < 0) ||
		(req.TranslateLookahead != nil && *req.TranslateLookahead < 0) {
		writeErr(w, http.StatusBadRequest, "lookahead values must be >= 0 (0 means unlimited)")
		return
	}

	if err := applyNovelSettings(r.Context(), a.store.db, novelID, req.IngestLookahead, req.TranslateLookahead); err != nil {
		log.Printf("patchNovelSettings: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not update novel settings")
		return
	}

	var settings novelSettingsResp
	if err := a.store.db.QueryRow(r.Context(),
		`SELECT ingest_lookahead, translate_lookahead FROM novel WHERE id = $1`, novelID,
	).Scan(&settings.IngestLookahead, &settings.TranslateLookahead); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeErr(w, http.StatusNotFound, "novel not found")
			return
		}
		log.Printf("patchNovelSettings readback: %v", err)
		writeErr(w, http.StatusInternalServerError, "saved but readback failed")
		return
	}
	settings.NovelID = novelID
	writeJSON(w, http.StatusOK, settings)
}

type novelSettingsResp struct {
	NovelID            string `json:"novel_id"`
	IngestLookahead    int    `json:"ingest_lookahead"`
	TranslateLookahead int    `json:"translate_lookahead"`
}

type translateAheadReq struct {
	// From is the first chapter index to consider; Count how many to look at from there.
	// Count omitted (or 0) uses the novel's own translate_lookahead (migration 0015).
	//
	// From omitted means "wherever the reader is": the window is placed just past the
	// furthest point any reader has reached, or at chapter 1 for a novel nobody has opened
	// yet. That lets a caller with no reader context — the scraper — keep the window
	// topped up without having to know or guess a position.
	From     *int `json:"from,omitempty"`
	Count    int  `json:"count,omitempty"`
	Priority bool `json:"priority,omitempty"`
}

type translateAheadResp struct {
	NovelID string `json:"novel_id"`
	// Queued lists only chapters this call actually moved into the queue — repeated calls
	// return an empty list rather than re-queueing, which is what makes it safe for the
	// reader to fire on every chapter turn.
	Queued      []int `json:"queued"`
	Prioritized bool  `json:"prioritized"`
}

// translateAhead queues translation for a window of chapters starting at From. Called as
// the reader advances, so translation follows the reader rather than racing the scraper
// through a whole novel (see store.queueTranslationRange).
func (a *API) translateAhead(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")

	var req translateAheadReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.From != nil && *req.From < 0 {
		writeErr(w, http.StatusBadRequest, "from must be >= 0")
		return
	}
	if req.Priority {
		if req.From == nil || req.Count != 1 {
			writeErr(w, http.StatusBadRequest, "priority requires an explicit from and count=1")
			return
		}
		prioritized, err := a.store.prioritizeChapter(r.Context(), novelID, *req.From)
		if errors.Is(err, pgx.ErrNoRows) {
			writeErr(w, http.StatusNotFound, "chapter not found")
			return
		}
		if err != nil {
			log.Printf("prioritize chapter: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not prioritize chapter")
			return
		}
		writeJSON(w, http.StatusOK, translateAheadResp{NovelID: novelID, Queued: []int{}, Prioritized: prioritized})
		return
	}

	from := 1
	if req.From != nil {
		from = *req.From
	} else {
		// No position given: place the window just past the furthest reader. This is the
		// path the scraper uses — it has no reader context, and asking it to guess would
		// either re-queue already-translated chapters or leave a gap.
		reached, err := a.store.furthestReaderPosition(r.Context(), novelID)
		if err != nil {
			log.Printf("translateAhead reader position: %v", err)
			writeErr(w, http.StatusInternalServerError, "lookup failed")
			return
		}
		from = reached + 1
	}

	count := req.Count
	if count <= 0 {
		lookahead, err := a.store.novelLookahead(r.Context(), novelID)
		if errors.Is(err, pgx.ErrNoRows) {
			writeErr(w, http.StatusNotFound, "novel not found")
			return
		}
		if err != nil {
			log.Printf("translateAhead lookahead: %v", err)
			writeErr(w, http.StatusInternalServerError, "lookup failed")
			return
		}
		count = lookahead
	}

	queued, err := a.store.queueTranslationRange(r.Context(), novelID, from, count)
	if err != nil {
		log.Printf("translateAhead: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not queue translation")
		return
	}
	writeJSON(w, http.StatusOK, translateAheadResp{NovelID: novelID, Queued: queued})
}

type correctGlossaryTermReq struct {
	TargetTerm string `json:"target_term"`
	AtChapter  int    `json:"at_chapter"`
}

type correctGlossaryTermResp struct {
	NovelID    string `json:"novel_id"`
	SourceTerm string `json:"source_term"`
	TargetTerm string `json:"target_term"`
	Version    int    `json:"version"`
}

// correctGlossaryTerm handles PATCH /novels/{id}/glossary/{term} — a human correcting a
// term the pipeline locked (PLAN.md Phase N2). Forward-only: this changes the term for
// chapters translated from here on; chapters already translated with the old term are
// unaffected (the retro-update engine that would fix those is a separate, unbuilt
// milestone — instructions.md/PLAN.md Milestone 3.1).
func (a *API) correctGlossaryTerm(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")
	sourceTerm := r.PathValue("term")

	var req correctGlossaryTermReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	req.TargetTerm = strings.TrimSpace(req.TargetTerm)
	if req.TargetTerm == "" {
		writeErr(w, http.StatusBadRequest, "target_term is required")
		return
	}
	if req.AtChapter < 0 {
		writeErr(w, http.StatusBadRequest, "at_chapter must be >= 0")
		return
	}

	version, err := a.store.CorrectGlossaryTerm(r.Context(), novelID, sourceTerm, req.TargetTerm, req.AtChapter)
	if errors.Is(err, ErrGlossaryTermConflict) {
		writeErr(w, http.StatusConflict, err.Error())
		return
	}
	if errors.Is(err, ErrGlossaryTermNotFound) {
		writeErr(w, http.StatusNotFound, "no such glossary term")
		return
	}
	if err != nil {
		log.Printf("correctGlossaryTerm: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not correct glossary term")
		return
	}
	writeJSON(w, http.StatusOK, correctGlossaryTermResp{
		NovelID: novelID, SourceTerm: sourceTerm, TargetTerm: req.TargetTerm, Version: version,
	})
}

func (a *API) deleteGlossaryTerm(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AtChapter int `json:"at_chapter"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.AtChapter < 0 {
		writeErr(w, http.StatusBadRequest, "a nonnegative at_chapter is required")
		return
	}
	version, err := a.store.DeleteGlossaryTerm(r.Context(), r.PathValue("id"), r.PathValue("term"), req.AtChapter)
	if errors.Is(err, ErrGlossaryTermNotFound) {
		writeErr(w, http.StatusNotFound, "no such glossary term")
		return
	}
	if err != nil {
		log.Printf("delete glossary: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not delete glossary term")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": true, "version": version})
}

type bootstrapGlossaryReq struct {
	Terms []bootstrapGlossaryTermReq `json:"terms"`
}

type bootstrapGlossaryTermReq struct {
	SourceTerm string `json:"source_term"`
	TargetTerm string `json:"target_term"`
}

type confirmGlossaryTermReq struct {
	SourceTerm string `json:"source_term"`
	TargetTerm string `json:"target_term"`
	AtChapter  int    `json:"at_chapter"`
	TermRole   string `json:"term_role"`
}

func (a *API) confirmGlossaryTerm(w http.ResponseWriter, r *http.Request) {
	var req confirmGlossaryTermReq
	if err := decodeJSONBody(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	req.SourceTerm = strings.TrimSpace(req.SourceTerm)
	version, err := a.store.ConfirmGlossaryTerm(r.Context(), r.PathValue("id"), req.SourceTerm,
		req.TargetTerm, req.AtChapter, req.TermRole)
	if errors.Is(err, ErrGlossaryTermInvalid) || errors.Is(err, ErrGlossaryTermConflict) {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, correctGlossaryTermResp{
		NovelID: r.PathValue("id"), SourceTerm: req.SourceTerm,
		TargetTerm: strings.TrimSpace(req.TargetTerm), Version: version,
	})
}

type bootstrapGlossaryResp struct {
	NovelID string                    `json:"novel_id"`
	Terms   []correctGlossaryTermResp `json:"terms"`
}

// bootstrapGlossary handles POST /novels/{id}/glossary/bootstrap — a human seeding the
// glossary from an existing (paired raw + fan-translated) bootstrap paste, before any
// chapter has actually been through RESOLVE (PLAN.md Phase N6). Each term locks via the
// same path resolve.py's _lock_glossary itself takes (see glossary.go's
// BootstrapGlossaryTerm) with entity_id left NULL until RESOLVE creates the real entity.
//
// Each term is its own BootstrapGlossaryTerm call/transaction (not one all-or-nothing
// transaction for the whole list) — but this handler still stops at the first failure,
// so terms before it in the request are already locked while terms after it are not. A
// re-submission of the same list is safe (every earlier term's call is now a no-op, per
// BootstrapGlossaryTerm's own idempotency), which is the intended recovery path rather
// than rollback.
func (a *API) bootstrapGlossary(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")

	var req bootstrapGlossaryReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if len(req.Terms) == 0 {
		writeErr(w, http.StatusBadRequest, "terms must be non-empty")
		return
	}

	results := make([]correctGlossaryTermResp, 0, len(req.Terms))
	for _, term := range req.Terms {
		term.SourceTerm = strings.TrimSpace(term.SourceTerm)
		term.TargetTerm = strings.TrimSpace(term.TargetTerm)
		if term.SourceTerm == "" || term.TargetTerm == "" {
			writeErr(w, http.StatusBadRequest, "source_term and target_term are required for every entry")
			return
		}
		version, err := a.store.BootstrapGlossaryTerm(r.Context(), novelID, term.SourceTerm, term.TargetTerm)
		// A surface that cannot be locked safely is the caller's input problem, not a
		// server fault: say so as a 400 with the reason, rather than a bare 500.
		if errors.Is(err, ErrGlossaryTermInvalid) {
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		if err != nil && !errors.Is(err, ErrGlossaryTermConflict) {
			log.Printf("bootstrapGlossary: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not seed glossary term "+term.SourceTerm)
			return
		}
		if errors.Is(err, ErrGlossaryTermConflict) {
			writeErr(w, http.StatusConflict, err.Error())
			return
		}
		results = append(results, correctGlossaryTermResp{
			NovelID: novelID, SourceTerm: term.SourceTerm, TargetTerm: term.TargetTerm, Version: version,
		})
	}
	writeJSON(w, http.StatusCreated, bootstrapGlossaryResp{NovelID: novelID, Terms: results})
}

// healthz is a cheap liveness probe used by compose and manual sanity checks.
func (a *API) healthz(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	if err := a.store.db.Ping(ctx); err != nil {
		writeErr(w, http.StatusServiceUnavailable, "postgres unreachable")
		return
	}
	if err := a.store.redis.Ping(ctx).Err(); err != nil {
		writeErr(w, http.StatusServiceUnavailable, "redis unreachable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
