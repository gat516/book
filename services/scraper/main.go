package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"
)

func main() {
	cfg := loadConfig()
	worker, err := NewWorker(cfg)
	if err != nil {
		log.Fatalf("startup: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := worker.RecoverProcessing(ctx); err != nil {
		log.Printf("recover interrupted scrape jobs: %v", err)
	}

	log.Printf("scraper worker draining %s", pendingQueue)
	if err := worker.Loop(ctx); err != nil {
		log.Fatalf("worker: %v", err)
	}
}
