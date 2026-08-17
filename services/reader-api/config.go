package main

import (
	"os"
	"strconv"
)

type Config struct {
	ListenAddr          string
	ReaderDatabaseURL   string
	ProgressDatabaseURL string
	AskAIURL            string
	AskAIInternalToken  string
	AskAITimeoutSeconds int
}

func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func loadConfig() Config {
	baseURL := getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine")
	timeout, err := strconv.Atoi(getenv("ASKAI_TIMEOUT_SECONDS", "120"))
	if err != nil || timeout <= 0 {
		timeout = 120
	}
	return Config{
		ListenAddr:          getenv("READER_LISTEN_ADDR", ":8081"),
		ReaderDatabaseURL:   getenv("READER_DATABASE_URL", baseURL),
		ProgressDatabaseURL: getenv("PROGRESS_DATABASE_URL", baseURL),
		AskAIURL:            getenv("ASKAI_URL", "http://localhost:8082"),
		AskAIInternalToken:  os.Getenv("ASKAI_INTERNAL_TOKEN"),
		AskAITimeoutSeconds: timeout,
	}
}
