package main

import (
	"os"
	"strconv"
	"time"
)

type Config struct {
	DatabaseURL  string
	RedisURL     string
	IngestAPIURL string
	// MinDelay/MaxDelay bound the randomized wait between two requests to one host. A
	// range rather than a rate: a steady tick is a machine signature, and pacing from the
	// end of the previous response means a slow site is asked for less, not more.
	MinDelay time.Duration
	MaxDelay time.Duration
	// CatchUpDelay paces the page a reader is actually waiting on -- they have read
	// everything stored, so this one page is fetched promptly instead of on the normal
	// gap. Capped at MinDelay; a site's Crawl-delay still wins over it.
	CatchUpDelay    time.Duration
	UserAgentString string
	ContentLenFloor int
	// HTTPAddr serves the preview endpoint (preview.go) beside the worker loop, so the
	// reader can see what a URL extracts to before committing a scrape to it.
	HTTPAddr string
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
	// MaxConcurrentJobs is how many books may be scraped at once. A walk ends only at the
	// end of a novel, so one job at a time means the second book never starts.
	MaxConcurrentJobs int
}

func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
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
		DatabaseURL:       getenv("DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"),
		RedisURL:          getenv("REDIS_URL", "redis://localhost:6379"),
		IngestAPIURL:      getenv("INGEST_API_URL", "http://localhost:8080"),
		MinDelay:          time.Duration(getenvInt("SCRAPE_MIN_DELAY_MS", 15000)) * time.Millisecond,
		MaxDelay:          time.Duration(getenvInt("SCRAPE_MAX_DELAY_MS", 40000)) * time.Millisecond,
		CatchUpDelay:      time.Duration(getenvInt("SCRAPE_CATCHUP_DELAY_MS", 3000)) * time.Millisecond,
		UserAgentString:   getenv("SCRAPE_USER_AGENT", "novel-engine-scraper/0.1 (+personal reading tool)"),
		ContentLenFloor:   getenvInt("CONTENT_LEN_FLOOR", 200),
		HTTPAddr:          getenv("SCRAPER_HTTP_ADDR", ":8083"),
		MaxQueueDepth:     getenvInt("SCRAPE_MAX_QUEUE_DEPTH", 50),
		MaxConcurrentJobs: getenvInt("SCRAPE_MAX_CONCURRENT_JOBS", 3),
	}
}
