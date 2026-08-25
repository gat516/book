package main

import (
	"os"
	"strconv"
)

type Config struct {
	DatabaseURL     string
	RedisURL        string
	IngestAPIURL    string
	ReqsPerSecond   float64
	JitterMillis    int
	UserAgentString string
	ContentLenFloor int
}

func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func getenvFloat(key string, fallback float64) float64 {
	if value := os.Getenv(key); value != "" {
		if parsed, err := strconv.ParseFloat(value, 64); err == nil {
			return parsed
		}
	}
	return fallback
}

func getenvInt(key string, fallback int) int {
	if value := os.Getenv(key); value != "" {
		if parsed, err := strconv.Atoi(value); err == nil {
			return parsed
		}
	}
	return fallback
}

func loadConfig() Config {
	return Config{
		DatabaseURL:     getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"),
		RedisURL:        getenv("REDIS_URL", "redis://localhost:6379"),
		IngestAPIURL:    getenv("INGEST_API_URL", "http://localhost:8080"),
		ReqsPerSecond:   getenvFloat("SCRAPE_RATE_PER_SEC", 0.3),
		JitterMillis:    getenvInt("SCRAPE_JITTER_MS", 800),
		UserAgentString: getenv("SCRAPE_USER_AGENT", "novel-engine-scraper/0.1 (+personal reading tool)"),
		ContentLenFloor: getenvInt("CONTENT_LEN_FLOOR", 200),
	}
}
