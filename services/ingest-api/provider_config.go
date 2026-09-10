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
// config. It carries no key: secrets live in provider_credential, keyed by provider, and
// a novel names a provider rather than holding its own copy (migration 0080).
type ProviderConfigInput struct {
	Provider       string
	Model          string
	TranslateModel string
	ExtractModel   string
	BaseURL        string
}

// ProviderConfigView is the read shape. Nothing is masked any more -- every field here is
// a per-book choice the client sent and can send back unchanged.
type ProviderConfigView struct {
	Provider       string `json:"provider"`
	Model          string `json:"model,omitempty"`
	TranslateModel string `json:"translate_model,omitempty"`
	ExtractModel   string `json:"extract_model,omitempty"`
	BaseURL        string `json:"base_url,omitempty"`
}

// insertProviderConfig writes cfg's row inside tx — called from insertNovel's transaction
// (store.go) so a half-written provider config can never outlive a failed novel creation.
func insertProviderConfig(ctx context.Context, tx pgx.Tx, novelID string, cfg ProviderConfigInput) error {
	var modelArg, baseURLArg any
	if cfg.Model != "" {
		modelArg = cfg.Model
	}
	if cfg.BaseURL != "" {
		baseURLArg = cfg.BaseURL
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO novel_provider_config (novel_id, provider, model, translate_model, extract_model, base_url)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		novelID, cfg.Provider, modelArg, nullableText(cfg.TranslateModel), nullableText(cfg.ExtractModel), baseURLArg,
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
	var model, translateModel, extractModel, baseURL *string
	err := s.db.QueryRow(ctx,
		`SELECT provider, model, translate_model, extract_model, base_url FROM novel_provider_config WHERE novel_id = $1`,
		novelID,
	).Scan(&v.Provider, &model, &translateModel, &extractModel, &baseURL)
	if errors.Is(err, pgx.ErrNoRows) {
		return ProviderConfigView{}, ErrProviderConfigNotFound
	}
	if err != nil {
		return ProviderConfigView{}, fmt.Errorf("get novel_provider_config: %w", err)
	}
	if model != nil {
		v.Model = *model
	}
	if translateModel != nil {
		v.TranslateModel = *translateModel
	}
	if extractModel != nil {
		v.ExtractModel = *extractModel
	}
	if baseURL != nil {
		v.BaseURL = *baseURL
	}
	return v, nil
}

// UpsertProviderConfig replaces a novel's provider config wholesale (PATCH semantics).
// Every column takes EXCLUDED: each one is a value the client can read back and send
// again, so clearing any of them is a legitimate thing to express. That symmetry is only
// possible because the key is gone -- it was the single field a masked read could not
// round-trip, which is what forced the one-way COALESCE this used to carry (0080).
func (s *Store) UpsertProviderConfig(ctx context.Context, novelID string, cfg ProviderConfigInput) error {
	var modelArg, baseURLArg any
	if cfg.Model != "" {
		modelArg = cfg.Model
	}
	if cfg.BaseURL != "" {
		baseURLArg = cfg.BaseURL
	}
	_, err := s.db.Exec(ctx,
		`INSERT INTO novel_provider_config (novel_id, provider, model, translate_model, extract_model, base_url, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, now())
		 ON CONFLICT (novel_id) DO UPDATE SET
		   provider = EXCLUDED.provider,
		   model = EXCLUDED.model,
		   translate_model = EXCLUDED.translate_model,
		   extract_model = EXCLUDED.extract_model,
		   base_url = EXCLUDED.base_url,
		   updated_at = now()`,
		novelID, cfg.Provider, modelArg, nullableText(cfg.TranslateModel), nullableText(cfg.ExtractModel), baseURLArg,
	)
	if err != nil {
		return fmt.Errorf("upsert novel_provider_config: %w", err)
	}
	return nil
}

func nullableText(value string) any {
	if value == "" {
		return nil
	}
	return value
}
