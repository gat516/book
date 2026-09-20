// Command ingest-api is the paste-ingest entrypoint (instructions.md §7.1). It accepts a
// pasted chapter, stores the body in the object store, records a chapter row in Postgres,
// and signals the offline pipeline via Redis. It is a writer service — no auth/gate here
// (that lives in reader-api, §8).
package main

import (
	"context"
	"github.com/jackc/pgx/v5"
	"log"
	"net/http"
	"novel-engine/platform/tenant"
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
	mux.Handle("GET /queue", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.queueControl)))
	mux.Handle("PATCH /queue", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.queueControl)))
	mux.Handle("POST /novels", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.createNovel)))
	mux.Handle("DELETE /novels/{id}", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.deleteNovel)))
	mux.HandleFunc("POST /novels/{id}/chapters", api.pasteChapter)
	mux.HandleFunc("PATCH /novels/{id}/glossary/{term}", api.correctGlossaryTerm)
	mux.HandleFunc("DELETE /novels/{id}/glossary/{term}", api.deleteGlossaryTerm)
	mux.HandleFunc("POST /novels/{id}/glossary/bootstrap", api.bootstrapGlossary)
	mux.HandleFunc("POST /novels/{id}/glossary/confirm", api.confirmGlossaryTerm)
	mux.HandleFunc("POST /novels/{id}/name-reviews/{term}/approve", api.approveCharacterName)
	mux.HandleFunc("POST /novels/{id}/translate-ahead", api.translateAhead)
	// Records maintenance. Internal-token gated like every other write; reader-api
	// proxies these on the operator's behalf.
	mux.Handle("POST /novels/{id}/chapter/{n}/facts/retry", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.factsRetry)))
	mux.Handle("POST /novels/{id}/chapter/{n}/facts/discard", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.factsDiscardChapter)))
	mux.Handle("POST /novels/{id}/facts/extract", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.factsExtract)))
	mux.Handle("POST /novels/{id}/facts/stop", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.factsStop)))
	mux.Handle("GET /novels/{id}/facts/status", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.getFactsStatus)))
	mux.Handle("POST /novels/{id}/facts/retract", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.retractFact)))
	mux.HandleFunc("PATCH /novels/{id}/settings", api.patchNovelSettings)
	mux.HandleFunc("GET /novels/{id}/provider-config", api.getProviderConfig)
	mux.HandleFunc("PATCH /novels/{id}/provider-config", api.putProviderConfig)
	mux.HandleFunc("GET /novels/{id}/provider-config/ollama-models", api.listOllamaModels)
	mux.HandleFunc("GET /novels/{id}/provider-health", api.providerHealth)
	// Global provider credentials (migration 0035): shared by every novel, so a key is
	// entered once rather than re-pasted per book. A novel may still override with its own.
	mux.Handle("GET /embedding-config", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.getEmbeddingConfig)))
	mux.Handle("PUT /embedding-config", requireInternalToken(cfg.IngestInternalToken, http.HandlerFunc(api.putEmbeddingConfig)))
	mux.HandleFunc("GET /provider-credentials", api.listProviderCredentials)
	mux.HandleFunc("PUT /provider-credentials/{provider}", api.putProviderCredential)
	mux.HandleFunc("DELETE /provider-credentials/{provider}", api.deleteProviderCredential)
	mux.HandleFunc("GET /healthz", api.healthz)

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           tenant.Internal(cfg.IngestInternalToken, mux),
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
	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}
	tenant.ConfigurePool(poolConfig)
	if tenant.Hosted() {
		poolConfig.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
			_, err := c.Exec(ctx, "SET ROLE ingest_writer")
			return err
		}
	}
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
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
