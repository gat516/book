package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
)

type embeddingConfig struct {
	Provider string `json:"provider"`
	Model    string `json:"model"`
}

func (c *embeddingConfig) validate() bool {
	c.Model = strings.TrimSpace(c.Model)
	switch c.Provider {
	case "auto", "disabled", "server":
		c.Model = ""
	case "gemini", "openrouter":
		if c.Model == "" || len(c.Model) > 200 || strings.ContainsAny(c.Model, "\r\n\t ") {
			return false
		}
	default:
		return false
	}
	return true
}

func (a *API) getEmbeddingConfig(w http.ResponseWriter, r *http.Request) {
	// With no saved override, preserve explicit operator configuration from env.
	c := embeddingConfig{Provider: "server"}
	err := a.store.db.QueryRow(r.Context(), "SELECT provider, model FROM embedding_config WHERE singleton").Scan(&c.Provider, &c.Model)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		writeErr(w, http.StatusInternalServerError, "could not read semantic search settings")
		return
	}
	writeJSON(w, http.StatusOK, c)
}

func (a *API) putEmbeddingConfig(w http.ResponseWriter, r *http.Request) {
	var c embeddingConfig
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&c); err != nil || !c.validate() {
		writeErr(w, http.StatusBadRequest, "choose auto, disabled, server, gemini, or openrouter; hosted providers require a model")
		return
	}
	_, err := a.store.db.Exec(r.Context(), `INSERT INTO embedding_config(singleton,provider,model)
	 VALUES(true,$1,$2) ON CONFLICT(singleton) DO UPDATE
	 SET provider=EXCLUDED.provider, model=EXCLUDED.model, updated_at=now()`, c.Provider, c.Model)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "could not save semantic search settings")
		return
	}
	writeJSON(w, http.StatusOK, c)
}
