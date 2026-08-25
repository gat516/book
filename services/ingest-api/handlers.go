package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
)

// API holds the dependencies the HTTP handlers need.
type API struct {
	store *Store
	cfg   Config
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
}

type providerConfigReq struct {
	Provider string `json:"provider"` // anthropic|deepseek|ollama
	Model    string `json:"model,omitempty"`
	BaseURL  string `json:"base_url,omitempty"`
	APIKey   string `json:"api_key,omitempty"` // plaintext in the request; never stored as such
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
}

type pasteChapterResp struct {
	NovelID      string `json:"novel_id"`
	ChapterIndex int    `json:"chapter_index"`
	RawHash      string `json:"raw_hash"`
	Status       string `json:"status"`
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
			if errors.Is(err, ErrProviderConfigKeyNotSet) {
				writeErr(w, http.StatusServiceUnavailable, "server is not configured to accept provider_config")
				return
			}
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		providerConfig = &pc
	}

	ont := ontologyForGenre(req.Genre)
	id, err := a.store.insertNovel(r.Context(), req.Title, req.SourceLang, req.TargetLang, req.Genre, ont, providerConfig)
	if err != nil {
		log.Printf("createNovel: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not create novel")
		return
	}
	writeJSON(w, http.StatusCreated, createNovelResp{ID: id})
}

// buildProviderConfigInput validates req and encrypts its API key (if any) under the
// server's INGEST_PROVIDER_CONFIG_KEY. Returns ErrProviderConfigKeyNotSet if the request
// needs the key but the server has none configured.
func (a *API) buildProviderConfigInput(req providerConfigReq) (ProviderConfigInput, error) {
	switch req.Provider {
	case "anthropic", "deepseek", "ollama":
	default:
		return ProviderConfigInput{}, fmt.Errorf("provider must be one of anthropic, deepseek, ollama")
	}

	in := ProviderConfigInput{Provider: req.Provider, Model: req.Model, BaseURL: req.BaseURL}
	if req.APIKey != "" {
		if !a.cfg.ProviderConfigKeySet {
			return ProviderConfigInput{}, ErrProviderConfigKeyNotSet
		}
		cipher, nonce, err := encryptProviderConfig([]byte(req.APIKey), a.cfg.ProviderConfigKey)
		if err != nil {
			return ProviderConfigInput{}, fmt.Errorf("encrypt api_key: %w", err)
		}
		in.APIKeyCipher, in.APIKeyNonce = cipher, nonce
	}
	return in, nil
}

// getProviderConfig handles GET /novels/{id}/provider-config — masked read, never
// decrypts (returns api_key_set: bool, not the key).
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
// provider config wholesale, re-encrypting the API key if one is supplied.
func (a *API) putProviderConfig(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")
	var req providerConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	in, err := a.buildProviderConfigInput(req)
	if err != nil {
		if errors.Is(err, ErrProviderConfigKeyNotSet) {
			writeErr(w, http.StatusServiceUnavailable, "server is not configured to accept provider_config")
			return
		}
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

	// raw_hash is the dedup / cache key (§3.1); prefix with the algorithm per the spec.
	sum := sha256.Sum256([]byte(req.RawText))
	rawHash := "sha256:" + hex.EncodeToString(sum[:])

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

	env := ChapterEnvelope{
		NovelID:      novelID,
		ChapterIndex: req.ChapterIndex,
		RawText:      req.RawText,
		SourceLang:   sourceLang,
		SourceMeta: SourceMeta{
			FetchedAt:     time.Now().UTC().Format(time.RFC3339),
			RawHash:       rawHash,
			Adapter:       "paste",
			SiteChapterNo: req.SiteChapterNo,
		},
	}

	if err := a.store.insertChapter(r.Context(), env, rawURI, translatedURI, translatedBy); err != nil {
		log.Printf("pasteChapter insert: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not record chapter")
		return
	}

	// Signal the pipeline. We enqueue even on re-paste so a stalled pipeline can be
	// retriggered; the pipeline itself dedups on content (§6.1).
	if err := a.store.enqueue(r.Context(), QueueMessage{NovelID: novelID, ChapterIndex: req.ChapterIndex}); err != nil {
		log.Printf("pasteChapter enqueue: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not enqueue job")
		return
	}

	writeJSON(w, http.StatusAccepted, pasteChapterResp{
		NovelID:      novelID,
		ChapterIndex: req.ChapterIndex,
		RawHash:      rawHash,
		Status:       "ingested",
	})
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
	if req.TargetTerm == "" {
		writeErr(w, http.StatusBadRequest, "target_term is required")
		return
	}
	if req.AtChapter < 0 {
		writeErr(w, http.StatusBadRequest, "at_chapter must be >= 0")
		return
	}

	version, err := a.store.CorrectGlossaryTerm(r.Context(), novelID, sourceTerm, req.TargetTerm, req.AtChapter)
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

type bootstrapGlossaryReq struct {
	Terms []bootstrapGlossaryTermReq `json:"terms"`
}

type bootstrapGlossaryTermReq struct {
	SourceTerm string `json:"source_term"`
	TargetTerm string `json:"target_term"`
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
		if term.SourceTerm == "" || term.TargetTerm == "" {
			writeErr(w, http.StatusBadRequest, "source_term and target_term are required for every entry")
			return
		}
		version, err := a.store.BootstrapGlossaryTerm(r.Context(), novelID, term.SourceTerm, term.TargetTerm)
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
