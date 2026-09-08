package main

// Phase D: the held-knowledge review workspace. A chapter's extracted facts, relations
// and occurrences land review_state='held' (migration 0074) and stay invisible to every
// normal reader/AskAI path until a human passes them here. This file is the read half
// (getHeldKnowledge, backed by the reader_held_knowledge definer function on the
// RLS-gated pool) and the write half (postKnowledgeReview, a thin authorize-and-proxy to
// ingest-api's token-gated route — "Go never reimplements a gate": the transactional
// stale-check, scope validation, idempotency and corroboration query all live in Go on
// the ingest-api side because that is the writer service, not because the gate logic
// belongs to a different language than the rest of this file).

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"
)

// getHeldKnowledge is the reviewer's read surface for one chapter. Deliberately no
// operator "show me everything pending" view exists anywhere in this API — a reviewer
// only ever sees held knowledge at or below their own stored reading position, the same
// gate every other reader-api response obeys (plan §0.3).
func (a *API) getHeldKnowledge(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	items, err := a.store.ListHeldKnowledge(r.Context(), novel, chapter, at)
	if err != nil {
		log.Printf("list held knowledge: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load held knowledge")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, HeldKnowledgeResponse{NovelID: novel, ChapterIndex: chapter, At: at, Items: items})
}

// postKnowledgeReview authorizes at the reader's own stored progress — never a
// client-supplied chapter — and then proxies to ingest-api's token-gated route, the same
// pattern as mutateFact/chapterKnowledgeMutation. The browser never sees the internal
// service token; reader-api's own authorization here is this repo's established
// single-operator convention (X-Reader-ID), not a stronger operator credential (see
// repair.go's NOTE on the same tradeoff).
func (a *API) postKnowledgeReview(w http.ResponseWriter, r *http.Request) {
	// The path chapter is the review's scope; gateAt refuses anything past the reader's
	// own stored progress. A verdict on a chapter nobody has authorized reading yet is
	// exactly the "operator sees everything pending" hole the plan calls out.
	actor, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 || chapter > at {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, paramsRequestLimit))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	var payload map[string]json.RawMessage
	if err = json.Unmarshal(body, &payload); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	// actor and chapter are server-authorized, never trusted from the body.
	encodedActor, _ := json.Marshal(actor)
	encodedChapter, _ := json.Marshal(chapter)
	payload["actor"] = encodedActor
	payload["chapter"] = encodedChapter
	body, _ = json.Marshal(payload)
	result, status, err := a.ingest.ReviewChapterKnowledge(r.Context(), novel, strconv.Itoa(chapter), body)
	if err != nil {
		log.Printf("review chapter knowledge: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
