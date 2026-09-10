package main

import (
	"encoding/base64"
	"log"
	"net/url"
	"os"
	"strings"
)

// Config holds everything the service needs to reach its backing stores. Values come
// from the environment (see .env.example / deploy/docker-compose.yml); each has a
// localhost default so `go run .` works against the compose stack with no setup.
type Config struct {
	ListenAddr  string // HTTP listen address
	DatabaseURL string // Postgres connection string
	RedisURL    string // Redis connection string (redis://host:port)

	// Object store (MinIO locally, S3 in prod — same S3 API).
	ObjectEndpoint  string // host:port, no scheme (minio-go takes Secure separately)
	ObjectAccessKey string
	ObjectSecretKey string
	ObjectBucket    string // bucket that holds raw chapter bodies
	ObjectUseSSL    bool

	// IngestInternalToken gates POST /novels — the one route reader-api proxies for the
	// browser (novel creation). Not general auth: ingest-api otherwise stays the "no
	// auth/gate here" writer service its own doc comment describes.
	IngestInternalToken string

	// ProviderConfigKey encrypts/decrypts provider_credential.api_key_cipher (migrations
	// 0035, 0080). Zero value (unset) is valid at startup — an all-Ollama install stores
	// no key at all — but any request that saves or uses one requires this to be set;
	// see ErrProviderConfigKeyNotSet.
	ProviderConfigKey    [32]byte
	ProviderConfigKeySet bool
	// OllamaAllowedHosts is an operator-controlled SSRF boundary for the model catalog
	// probe. The browser can select a saved URL, never an arbitrary request path.
	OllamaAllowedHosts map[string]bool
	// OllamaHost is the operator-selected server endpoint. It may be a loopback SSH
	// tunnel to a GPU host; model discovery must use the same endpoint as the pipeline.
	OllamaHost string
	// ProviderHealthAllowedHosts is the explicit SSRF boundary for hosted provider
	// catalog probes. Official provider hosts are included by default; custom account or
	// novel endpoints require an operator entry in PROVIDER_HEALTH_ALLOWED_HOSTS.
	ProviderHealthAllowedHosts map[string]bool
}

// getenv returns the env var if set and non-empty, otherwise the fallback.
func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// loadConfig reads configuration from the environment with compose-friendly defaults.
func loadConfig() Config {
	cfg := Config{
		ListenAddr:  getenv("LISTEN_ADDR", ":8080"),
		DatabaseURL: getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"),
		RedisURL:    getenv("REDIS_URL", "redis://localhost:6379"),

		// OBJECT_STORE_ENDPOINT in .env.example is a URL (http://localhost:9000); minio-go
		// wants a bare host:port, so we default to that form here.
		ObjectEndpoint:  getenv("OBJECT_STORE_ENDPOINT", "localhost:9000"),
		ObjectAccessKey: getenv("OBJECT_STORE_ACCESS_KEY", "minio"),
		ObjectSecretKey: getenv("OBJECT_STORE_SECRET_KEY", "minio12345"),
		ObjectBucket:    getenv("OBJECT_STORE_BUCKET", "raw-chapters"),
		ObjectUseSSL:    os.Getenv("OBJECT_STORE_USE_SSL") == "true",

		IngestInternalToken: os.Getenv("INGEST_INTERNAL_TOKEN"),
		OllamaAllowedHosts:  map[string]bool{},
		OllamaHost:          getenv("OLLAMA_HOST", "http://localhost:11434"),
		ProviderHealthAllowedHosts: map[string]bool{
			"api.anthropic.com":                 true,
			"api.deepseek.com":                  true,
			"api.groq.com":                      true,
			"generativelanguage.googleapis.com": true,
		},
	}
	for _, host := range strings.Split(getenv("OLLAMA_ALLOWED_HOSTS", "localhost,127.0.0.1"), ",") {
		if host = strings.TrimSpace(strings.ToLower(host)); host != "" {
			cfg.OllamaAllowedHosts[host] = true
		}
	}
	if parsed, err := url.Parse(cfg.OllamaHost); err == nil && parsed.Hostname() != "" {
		cfg.OllamaAllowedHosts[strings.ToLower(parsed.Hostname())] = true
	}
	for _, host := range strings.Split(os.Getenv("PROVIDER_HEALTH_ALLOWED_HOSTS"), ",") {
		if host = strings.TrimSpace(strings.ToLower(host)); host != "" {
			cfg.ProviderHealthAllowedHosts[host] = true
		}
	}
	// An explicitly configured process provider base is an operator choice and is
	// therefore admitted without duplicating it in the allowlist environment variable.
	for _, raw := range []string{os.Getenv("ANTHROPIC_BASE_URL"), os.Getenv("DEEPSEEK_BASE_URL"), os.Getenv("GEMINI_BASE_URL")} {
		if parsed, err := url.Parse(raw); err == nil && parsed.Hostname() != "" {
			cfg.ProviderHealthAllowedHosts[strings.ToLower(parsed.Hostname())] = true
		}
	}

	if raw := os.Getenv("INGEST_PROVIDER_CONFIG_KEY"); raw != "" {
		decoded, err := base64.StdEncoding.DecodeString(raw)
		if err != nil {
			log.Fatalf("startup: INGEST_PROVIDER_CONFIG_KEY is not valid base64: %v", err)
		}
		if len(decoded) != 32 {
			log.Fatalf("startup: INGEST_PROVIDER_CONFIG_KEY must decode to 32 bytes, got %d", len(decoded))
		}
		copy(cfg.ProviderConfigKey[:], decoded)
		cfg.ProviderConfigKeySet = true
	}

	return cfg
}
