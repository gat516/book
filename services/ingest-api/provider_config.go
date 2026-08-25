package main

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// ErrProviderConfigNotFound is returned by GetProviderConfig when a novel has no
// provider_config row — the zero-config default (fall back to the process-wide
// LLM_PROVIDER env var, PLAN.md Phase N4), not an error condition for callers to log.
var ErrProviderConfigNotFound = errors.New("no provider config for this novel")

// ProviderConfigInput is what a caller supplies to create/replace a novel's provider
// config. APIKeyCipher/APIKeyNonce are already-encrypted (see crypto.go) — this package
// never holds a plaintext key past the handler that called encryptProviderConfig.
type ProviderConfigInput struct {
	Provider     string
	Model        string
	BaseURL      string
	APIKeyCipher []byte
	APIKeyNonce  []byte
}

// ProviderConfigView is the masked read shape (§ Phase N3 "Done when"): never the key
// itself, only whether one is set.
type ProviderConfigView struct {
	Provider  string `json:"provider"`
	Model     string `json:"model,omitempty"`
	BaseURL   string `json:"base_url,omitempty"`
	APIKeySet bool   `json:"api_key_set"`
}

// insertProviderConfig writes cfg's row inside tx — called from insertNovel's transaction
// (store.go) so a half-written provider config can never outlive a failed novel creation.
func insertProviderConfig(ctx context.Context, tx pgx.Tx, novelID string, cfg ProviderConfigInput) error {
	var modelArg, baseURLArg, cipherArg, nonceArg any
	if cfg.Model != "" {
		modelArg = cfg.Model
	}
	if cfg.BaseURL != "" {
		baseURLArg = cfg.BaseURL
	}
	if cfg.APIKeyCipher != nil {
		cipherArg, nonceArg = cfg.APIKeyCipher, cfg.APIKeyNonce
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO novel_provider_config (novel_id, provider, model, base_url, api_key_cipher, api_key_nonce)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		novelID, cfg.Provider, modelArg, baseURLArg, cipherArg, nonceArg,
	)
	if err != nil {
		return fmt.Errorf("insert novel_provider_config: %w", err)
	}
	return nil
}

// GetProviderConfig returns the masked view of a novel's provider config, or
// ErrProviderConfigNotFound if the novel has none set.
func (s *Store) GetProviderConfig(ctx context.Context, novelID string) (ProviderConfigView, error) {
	var v ProviderConfigView
	var model, baseURL *string
	var cipher []byte
	err := s.db.QueryRow(ctx,
		`SELECT provider, model, base_url, api_key_cipher FROM novel_provider_config WHERE novel_id = $1`,
		novelID,
	).Scan(&v.Provider, &model, &baseURL, &cipher)
	if errors.Is(err, pgx.ErrNoRows) {
		return ProviderConfigView{}, ErrProviderConfigNotFound
	}
	if err != nil {
		return ProviderConfigView{}, fmt.Errorf("get novel_provider_config: %w", err)
	}
	if model != nil {
		v.Model = *model
	}
	if baseURL != nil {
		v.BaseURL = *baseURL
	}
	v.APIKeySet = cipher != nil
	return v, nil
}

// UpsertProviderConfig replaces a novel's provider config wholesale (PATCH semantics —
// the caller re-encrypts the key on every update; there is no partial-field merge, since
// a masked read can't tell the handler what the old plaintext key was anyway).
func (s *Store) UpsertProviderConfig(ctx context.Context, novelID string, cfg ProviderConfigInput) error {
	var modelArg, baseURLArg, cipherArg, nonceArg any
	if cfg.Model != "" {
		modelArg = cfg.Model
	}
	if cfg.BaseURL != "" {
		baseURLArg = cfg.BaseURL
	}
	if cfg.APIKeyCipher != nil {
		cipherArg, nonceArg = cfg.APIKeyCipher, cfg.APIKeyNonce
	}
	_, err := s.db.Exec(ctx,
		`INSERT INTO novel_provider_config (novel_id, provider, model, base_url, api_key_cipher, api_key_nonce, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, now())
		 ON CONFLICT (novel_id) DO UPDATE SET
		   provider = EXCLUDED.provider,
		   model = EXCLUDED.model,
		   base_url = EXCLUDED.base_url,
		   api_key_cipher = EXCLUDED.api_key_cipher,
		   api_key_nonce = EXCLUDED.api_key_nonce,
		   updated_at = now()`,
		novelID, cfg.Provider, modelArg, baseURLArg, cipherArg, nonceArg,
	)
	if err != nil {
		return fmt.Errorf("upsert novel_provider_config: %w", err)
	}
	return nil
}
