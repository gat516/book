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
	// MaxQueueDepth caps how far the scraper may run ahead of the pipeline. Fetching is
	// network-bound (order of a thousand chapters an hour) while the pipeline is LLM-bound
	// (order of tens), so without a limit a large novel is fetched in full within hours
	// onto a queue that then takes weeks to drain — while hammering the source site for
	// content nothing can process yet. Measured here: 145 chapters fetched in 30 minutes,
	// none translated.
	//
	// This PAUSES rather than stops: the walk resumes as the pipeline drains, so a scrape
	// still eventually covers the whole novel, just no faster than it can be consumed.
	MaxQueueDepth int
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
		MaxQueueDepth:   getenvInt("SCRAPE_MAX_QUEUE_DEPTH", 50),
	}
}
