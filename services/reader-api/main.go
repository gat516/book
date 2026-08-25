// Command reader-api serves spoiler-gated graph reads and reader progress.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

func main() {
	cfg := loadConfig()
	if cfg.AskAIInternalToken == "" {
		log.Fatal("startup: ASKAI_INTERNAL_TOKEN is required")
	}

	objects, err := minio.New(cfg.ObjectEndpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.ObjectAccessKey, cfg.ObjectSecretKey, ""),
		Secure: cfg.ObjectUseSSL,
	})
	if err != nil {
		log.Fatalf("startup: object store client: %v", err)
	}

	startupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	store, err := newStore(startupCtx, cfg, objects)
	cancel()
	if err != nil {
		log.Fatalf("startup: %v", err)
	}
	defer store.Close()

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           (&API{store: store, ask: newAskClient(cfg), ingest: newIngestClient(cfg)}).routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serverErrors := make(chan error, 1)
	go func() {
		log.Printf("reader-api listening on %s", cfg.ListenAddr)
		serverErrors <- server.ListenAndServe()
	}()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	select {
	case sig := <-signals:
		log.Printf("received %s; shutting down", sig)
	case err := <-serverErrors:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server: %v", err)
		}
		return
	}

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
