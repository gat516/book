package main

import "os"

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
	return Config{
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
	}
}
