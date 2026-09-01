package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"

	"github.com/jackc/pgx/v5"
)

// Provider credentials shared by every novel (migration 0035). A novel_provider_config
// row still chooses provider and model per book, and may carry its own key as an
// override; this is the fallback a book uses when it has none of its own.

var ErrProviderCredentialNotFound = errors.New("provider credential not found")

type providerCredentialReq struct {
	BaseURL string `json:"base_url,omitempty"`
	APIKey  string `json:"api_key,omitempty"` // plaintext in the request; never stored as such
}

// ProviderCredentialView is the masked read shape, matching ProviderConfigView's stance:
// never the key itself, only whether one is set.
type ProviderCredentialView struct {
	Provider  string `json:"provider"`
	BaseURL   string `json:"base_url,omitempty"`
	APIKeySet bool   `json:"api_key_set"`
}

type ProviderCredentialInput struct {
	Provider     string
	BaseURL      string
	APIKeyCipher []byte
	APIKeyNonce  []byte
}

func (s *Store) ListProviderCredentials(ctx context.Context) ([]ProviderCredentialView, error) {
	rows, err := s.db.Query(ctx,
		`SELECT provider, base_url, api_key_cipher FROM provider_credential ORDER BY provider`)
	if err != nil {
		return nil, fmt.Errorf("list provider_credential: %w", err)
	}
	defer rows.Close()

	// Never nil: the JSON response should be [] rather than null for an empty store.
	out := []ProviderCredentialView{}
	for rows.Next() {
		var v ProviderCredentialView
		var baseURL *string
		var cipher []byte
		if err := rows.Scan(&v.Provider, &baseURL, &cipher); err != nil {
			return nil, fmt.Errorf("scan provider_credential: %w", err)
		}
		if baseURL != nil {
			v.BaseURL = *baseURL
		}
		v.APIKeySet = cipher != nil
		out = append(out, v)
	}
	return out, rows.Err()
}

func (s *Store) UpsertProviderCredential(ctx context.Context, in ProviderCredentialInput) error {
	var baseURLArg, cipherArg, nonceArg any
	if in.BaseURL != "" {
		baseURLArg = in.BaseURL
	}
	if in.APIKeyCipher != nil {
		cipherArg, nonceArg = in.APIKeyCipher, in.APIKeyNonce
	}
	_, err := s.db.Exec(ctx,
		`INSERT INTO provider_credential (provider, base_url, api_key_cipher, api_key_nonce, updated_at)
		 VALUES ($1, $2, $3, $4, now())
		 ON CONFLICT (provider) DO UPDATE SET
		   base_url = EXCLUDED.base_url,
		   -- COALESCE for the same reason as novel_provider_config: the key is the one
		   -- field a client cannot read back, so an edit that omits it means "unchanged",
		   -- never "erase". Clearing a key is DELETE, which is explicit.
		   api_key_cipher = COALESCE(EXCLUDED.api_key_cipher, provider_credential.api_key_cipher),
		   api_key_nonce = COALESCE(EXCLUDED.api_key_nonce, provider_credential.api_key_nonce),
		   updated_at = now()`,
		in.Provider, baseURLArg, cipherArg, nonceArg,
	)
	if err != nil {
		return fmt.Errorf("upsert provider_credential: %w", err)
	}
	return nil
}

func (s *Store) DeleteProviderCredential(ctx context.Context, provider string) error {
	tag, err := s.db.Exec(ctx, `DELETE FROM provider_credential WHERE provider = $1`, provider)
	if err != nil {
		return fmt.Errorf("delete provider_credential: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrProviderCredentialNotFound
	}
	return nil
}

// GetProviderCredential returns one provider's stored base URL and decrypted key. Unlike
// every other read path this DOES decrypt, because it is what the pipeline's fallback
// resolution needs; it is never reachable from an HTTP handler.
func (s *Store) GetProviderCredential(ctx context.Context, provider string, key [32]byte) (baseURL string, apiKey string, err error) {
	var baseURLPtr *string
	var cipher, nonce []byte
	err = s.db.QueryRow(ctx,
		`SELECT base_url, api_key_cipher, api_key_nonce FROM provider_credential WHERE provider = $1`,
		provider,
	).Scan(&baseURLPtr, &cipher, &nonce)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", ErrProviderCredentialNotFound
	}
	if err != nil {
		return "", "", fmt.Errorf("get provider_credential: %w", err)
	}
	if baseURLPtr != nil {
		baseURL = *baseURLPtr
	}
	if cipher != nil {
		plain, err := decryptProviderConfig(cipher, nonce, key)
		if err != nil {
			return "", "", fmt.Errorf("decrypt provider_credential: %w", err)
		}
		apiKey = string(plain)
	}
	return baseURL, apiKey, nil
}

// listProviderCredentials handles GET /provider-credentials — masked, never decrypts.
func (a *API) listProviderCredentials(w http.ResponseWriter, r *http.Request) {
	views, err := a.store.ListProviderCredentials(r.Context())
	if err != nil {
		log.Printf("listProviderCredentials: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not read provider credentials")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"credentials": views})
}

// putProviderCredential handles PUT /provider-credentials/{provider}.
func (a *API) putProviderCredential(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	switch provider {
	case "anthropic", "deepseek", "gemini", "ollama":
	default:
		writeErr(w, http.StatusBadRequest, "provider must be one of anthropic, deepseek, gemini, ollama")
		return
	}
	var req providerCredentialReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	in := ProviderCredentialInput{Provider: provider, BaseURL: req.BaseURL}
	if req.APIKey != "" {
		if !a.cfg.ProviderConfigKeySet {
			writeErr(w, http.StatusServiceUnavailable, "server is not configured to accept API keys")
			return
		}
		cipher, nonce, err := encryptProviderConfig([]byte(req.APIKey), a.cfg.ProviderConfigKey)
		if err != nil {
			log.Printf("putProviderCredential encrypt: %v", err)
			writeErr(w, http.StatusInternalServerError, "could not encrypt api_key")
			return
		}
		in.APIKeyCipher, in.APIKeyNonce = cipher, nonce
	}
	if err := a.store.UpsertProviderCredential(r.Context(), in); err != nil {
		log.Printf("putProviderCredential: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not save provider credential")
		return
	}
	views, err := a.store.ListProviderCredentials(r.Context())
	if err != nil {
		log.Printf("putProviderCredential readback: %v", err)
		writeErr(w, http.StatusInternalServerError, "saved but readback failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"credentials": views})
}

// deleteProviderCredential handles DELETE /provider-credentials/{provider}. This is the
// only way to clear a stored key — an omitted key on PUT means "unchanged".
func (a *API) deleteProviderCredential(w http.ResponseWriter, r *http.Request) {
	err := a.store.DeleteProviderCredential(r.Context(), r.PathValue("provider"))
	if errors.Is(err, ErrProviderCredentialNotFound) {
		writeErr(w, http.StatusNotFound, "no credential for this provider")
		return
	}
	if err != nil {
		log.Printf("deleteProviderCredential: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not delete provider credential")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
