// Command ingest-api is the paste-ingest entrypoint (instructions.md §7.1). It accepts a
// pasted chapter, stores the body in the object store, records a chapter row in Postgres,
// and signals the offline pipeline via Redis. It is a writer service — no auth/gate here
// (that lives in reader-api, §8).
package main

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
)

func main() {
	cfg := loadConfig()
	if cfg.IngestInternalToken == "" {
		log.Fatal("startup: INGEST_INTERNAL_TOKEN is required")
	}

	// Fail fast if any backing store is unreachable — clearer than a first-request 500.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	store, err := newStore(ctx, cfg)
	if err != nil {
		log.Fatalf("startup: %v", err)
	}

	api := &API{store: store, cfg: cfg}

	// Go 1.22+ ServeMux supports method + path-parameter patterns, so we get routing
	// with zero dependencies. {id} is read in handlers via r.PathValue("id").
	mux := http.NewServeMux()
	mux.Handle("POST /novels", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.createNovel)))
	mux.HandleFunc("POST /novels/{id}/chapters", api.pasteChapter)
	mux.HandleFunc("PATCH /novels/{id}/glossary/{term}", api.correctGlossaryTerm)
	mux.HandleFunc("POST /novels/{id}/glossary/bootstrap", api.bootstrapGlossary)
	mux.HandleFunc("POST /novels/{id}/translate-ahead", api.translateAhead)
	mux.HandleFunc("GET /novels/{id}/provider-config", api.getProviderConfig)
	mux.HandleFunc("PATCH /novels/{id}/provider-config", api.putProviderConfig)
	mux.HandleFunc("GET /healthz", api.healthz)

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("ingest-api listening on %s", cfg.ListenAddr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server: %v", err)
	}
}

// newStore constructs and health-checks the three backing clients, and ensures the
// object-store bucket exists.
func newStore(ctx context.Context, cfg Config) (*Store, error) {
	// Postgres
	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, err
	}

	// Redis
	ropts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, err
	}
	rdb := redis.NewClient(ropts)
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, err
	}

	// Object store (MinIO / S3)
	mc, err := minio.New(cfg.ObjectEndpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.ObjectAccessKey, cfg.ObjectSecretKey, ""),
		Secure: cfg.ObjectUseSSL,
	})
	if err != nil {
		return nil, err
	}
	exists, err := mc.BucketExists(ctx, cfg.ObjectBucket)
	if err != nil {
		return nil, err
	}
	if !exists {
		if err := mc.MakeBucket(ctx, cfg.ObjectBucket, minio.MakeBucketOptions{}); err != nil {
			return nil, err
		}
		log.Printf("created object-store bucket %q", cfg.ObjectBucket)
	}

	return &Store{db: pool, redis: rdb, minio: mc, bucket: cfg.ObjectBucket}, nil
}
