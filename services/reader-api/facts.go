package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
)

// mutateFact keeps the browser outside the writer trust boundary. The observed revision
// and version remain in the body for ingest-api's transactional stale check; actor is
// always derived here rather than trusted from JSON.
func (a *API) mutateFact(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	factID := r.PathValue("fact")
	if parsed, err := strconv.ParseInt(factID, 10, 64); err != nil || parsed <= 0 {
		writeError(w, http.StatusBadRequest, "invalid fact id")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<16))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	var payload map[string]json.RawMessage
	if err = json.Unmarshal(body, &payload); err != nil {
		writeError(w, http.StatusBadRequest, "invalid fact edit")
		return
	}
	actor := "unknown reader"
	if id, found := readerID(r); found {
		actor = id
	}
	encoded, _ := json.Marshal(actor)
	payload["actor"] = encoded
	body, _ = json.Marshal(payload)
	suffix := "/display"
	if r.Method == http.MethodDelete {
		suffix = ""
	} else if strings.HasSuffix(r.URL.Path, "/corrections") {
		suffix = "/corrections"
	}
	result, status, err := a.ingest.MutateFact(r.Context(), r.Method, novelID, factID, suffix, body)
	if err != nil {
		log.Printf("mutate fact: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) chapterKnowledgeMutation(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	chapter := r.PathValue("n")
	if parsed, err := strconv.Atoi(chapter); err != nil || parsed < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request")
		return
	}
	var payload map[string]json.RawMessage
	if len(body) == 0 {
		payload = map[string]json.RawMessage{}
	} else if err = json.Unmarshal(body, &payload); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request")
		return
	}
	actor := "unknown operator"
	if id, found := readerID(r); found {
		actor = id
	}
	encoded, _ := json.Marshal(actor)
	payload["requested_by"] = encoded
	body, _ = json.Marshal(payload)
	result, status, err := a.ingest.ChapterKnowledgeMutation(r.Context(), novelID, chapter, r.PathValue("run"), body)
	if err != nil {
		log.Printf("chapter knowledge mutation: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
