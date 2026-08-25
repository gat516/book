package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
)

// ErrProviderConfigKeyNotSet is returned when a request includes a provider_config block
// but INGEST_PROVIDER_CONFIG_KEY isn't configured — provider config is optional per-novel
// (most novels have none), so this is only a startup-fatal condition once a request
// actually needs the key, not unconditionally like INGEST_INTERNAL_TOKEN.
var ErrProviderConfigKeyNotSet = errors.New("INGEST_PROVIDER_CONFIG_KEY is not set")

// encryptProviderConfig AES-GCM encrypts plaintext (a provider API key) under key,
// returning ciphertext and a fresh nonce. Application-level encryption, not pgcrypto
// (PLAN.md Phase N3): key material never lives in Postgres, so a pg_dump from any
// owner-role connection is never decryptable without this key held separately.
func encryptProviderConfig(plaintext []byte, key [32]byte) (ciphertext, nonce []byte, err error) {
	block, err := aes.NewCipher(key[:])
	if err != nil {
		return nil, nil, fmt.Errorf("new cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, fmt.Errorf("new gcm: %w", err)
	}
	nonce = make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, nil, fmt.Errorf("generate nonce: %w", err)
	}
	ciphertext = gcm.Seal(nil, nonce, plaintext, nil)
	return ciphertext, nonce, nil
}

// decryptProviderConfig is the inverse — used by the masked-read/update paths, which
// never actually need it (they return "api_key_set" not the key itself), but ingest-api
// is the role that holds the key, so re-encryption on update goes through here too via
// encrypt, not decrypt; kept for symmetry and any future debug tooling.
func decryptProviderConfig(ciphertext, nonce []byte, key [32]byte) ([]byte, error) {
	block, err := aes.NewCipher(key[:])
	if err != nil {
		return nil, fmt.Errorf("new cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("new gcm: %w", err)
	}
	plaintext, err := gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, fmt.Errorf("decrypt: %w", err)
	}
	return plaintext, nil
}
