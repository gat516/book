package main

import (
	"log"
	"net/http"
)

// Rebuild status is operational metadata, not knowledge: it is intentionally ungated
// and contains only generation ids and counts. The ingest proxy still requires its
// internal bearer token.
func (a *API) getRecordsRebuildStatus(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	if a.ingest == nil {
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	result, status, err := a.ingest.RecordsRebuildStatus(r.Context(), novelID)
	if err != nil {
		log.Printf("records rebuild status: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
