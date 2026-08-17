package main

import "os"

type Config struct {
	ListenAddr          string
	ReaderDatabaseURL   string
	ProgressDatabaseURL string
}

func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func loadConfig() Config {
	baseURL := getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine")
	return Config{
		ListenAddr:          getenv("READER_LISTEN_ADDR", ":8081"),
		ReaderDatabaseURL:   getenv("READER_DATABASE_URL", baseURL),
		ProgressDatabaseURL: getenv("PROGRESS_DATABASE_URL", baseURL),
	}
}
