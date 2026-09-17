package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEmbeddingConfigValidation(t *testing.T) {
	for _, body := range []string{
		`{"provider":"unknown"}`, `{"provider":"gemini"}`,
		`{"provider":"openrouter","model":"chat model"}`, `{broken`,
	} {
		w := httptest.NewRecorder()
		(&API{}).putEmbeddingConfig(w, httptest.NewRequest(http.MethodPut, "/embedding-config", strings.NewReader(body)))
		if w.Code != http.StatusBadRequest {
			t.Fatalf("%s: got %d", body, w.Code)
		}
	}
	for _, provider := range []string{"auto", "disabled", "server"} {
		c := embeddingConfig{Provider: provider, Model: "stale-model"}
		if !c.validate() || c.Model != "" {
			t.Fatalf("invalid normalization: %+v", c)
		}
	}
	c := embeddingConfig{Provider: "openrouter", Model: " openai/text-embedding-3-small "}
	if !c.validate() || c.Model != "openai/text-embedding-3-small" {
		t.Fatalf("invalid hosted model: %+v", c)
	}
}

func TestEmbeddingConfigPersistence(t *testing.T) {
	store := integrationStore(t)
	ctx := context.Background()
	var old embeddingConfig
	err := store.db.QueryRow(ctx, "SELECT provider,model FROM embedding_config WHERE singleton").Scan(&old.Provider, &old.Model)
	t.Cleanup(func() {
		if err == nil {
			_, _ = store.db.Exec(ctx, "UPDATE embedding_config SET provider=$1,model=$2 WHERE singleton", old.Provider, old.Model)
		} else {
			_, _ = store.db.Exec(ctx, "DELETE FROM embedding_config")
		}
	})
	api := &API{store: store}
	for _, body := range []string{`{"provider":"gemini","model":"gemini-embedding-001"}`, `{"provider":"disabled"}`} {
		w := httptest.NewRecorder()
		api.putEmbeddingConfig(w, httptest.NewRequest(http.MethodPut, "/embedding-config", strings.NewReader(body)))
		if w.Code != http.StatusOK {
			t.Fatalf("save: %d %s", w.Code, w.Body.String())
		}
		read := httptest.NewRecorder()
		api.getEmbeddingConfig(read, httptest.NewRequest(http.MethodGet, "/embedding-config", nil))
		var saved, got embeddingConfig
		_ = json.Unmarshal(w.Body.Bytes(), &saved)
		_ = json.Unmarshal(read.Body.Bytes(), &got)
		if read.Code != http.StatusOK || saved != got {
			t.Fatalf("readback: %d %s", read.Code, read.Body.String())
		}
	}
}
