package main

import (
	"context"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"strings"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// httpClient wraps http.Client with the politeness discipline instructions.md §7.2
// requires: rate limit + jitter + an honest User-Agent + a robots.txt check per host,
// done once here so every Site adapter gets it for free instead of re-implementing it.
type httpClient struct {
	inner     *http.Client
	limiter   *rate.Limiter
	jitterMs  int
	userAgent string

	mu     sync.Mutex
	robots map[string]*robotsRules // host -> parsed rules, fetched lazily and cached
}

func newHTTPClient(reqsPerSecond float64, jitterMs int, userAgent string) *httpClient {
	return &httpClient{
		inner:     &http.Client{Timeout: 30 * time.Second},
		limiter:   rate.NewLimiter(rate.Limit(reqsPerSecond), 1),
		jitterMs:  jitterMs,
		userAgent: userAgent,
		robots:    make(map[string]*robotsRules),
	}
}

// Get performs one polite GET: waits for the rate limiter (which already spaces requests
// out), adds a small extra jitter so requests aren't perfectly periodic, and refuses a
// path robots.txt disallows. Callers are responsible for backoff on error status codes —
// see fetch.go's retry loop, since what counts as "retry" vs. "stop" is adapter-specific
// (a 404 stops a walk, a 429 should back off and retry).
func (c *httpClient) Get(ctx context.Context, rawURL string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	allowed, err := c.robotsAllow(ctx, req.URL.Scheme+"://"+req.URL.Host, req.URL.Path)
	if err != nil {
		// A robots.txt fetch failure shouldn't block scraping (many sites have none) —
		// fail open on the check itself, not on the site's actual content.
		allowed = true
	}
	if !allowed {
		return nil, fmt.Errorf("robots.txt disallows %s", rawURL)
	}

	if err := c.limiter.Wait(ctx); err != nil {
		return nil, err
	}
	jitter := time.Duration(rand.Int64N(int64(c.jitterMs))) * time.Millisecond
	select {
	case <-time.After(jitter):
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	req.Header.Set("User-Agent", c.userAgent)
	return c.inner.Do(req)
}

type robotsRules struct {
	disallow []string // path prefixes disallowed for User-agent: * (the only group we honor)
}

func (c *httpClient) robotsAllow(ctx context.Context, origin, path string) (bool, error) {
	c.mu.Lock()
	rules, cached := c.robots[origin]
	c.mu.Unlock()
	if !cached {
		var err error
		rules, err = fetchRobots(ctx, c.inner, origin, c.userAgent)
		if err != nil {
			return true, err
		}
		c.mu.Lock()
		c.robots[origin] = rules
		c.mu.Unlock()
	}
	for _, prefix := range rules.disallow {
		if prefix != "" && strings.HasPrefix(path, prefix) {
			return false, nil
		}
	}
	return true, nil
}

// fetchRobots parses only what this scraper needs: User-agent: * / Disallow: lines. Not a
// general robots.txt implementation (no crawl-delay, no per-bot groups) — sufficient for
// "don't fetch a path the site has explicitly closed off."
func fetchRobots(ctx context.Context, client *http.Client, origin, userAgent string) (*robotsRules, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, origin+"/robots.txt", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return &robotsRules{}, nil // no robots.txt (or unreadable) => nothing disallowed
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}

	rules := &robotsRules{}
	applies := false
	for _, line := range strings.Split(string(body), "\n") {
		line = strings.TrimSpace(line)
		if idx := strings.Index(line, "#"); idx >= 0 {
			line = strings.TrimSpace(line[:idx])
		}
		switch {
		case strings.HasPrefix(strings.ToLower(line), "user-agent:"):
			agent := strings.TrimSpace(line[len("user-agent:"):])
			applies = agent == "*"
		case applies && strings.HasPrefix(strings.ToLower(line), "disallow:"):
			path := strings.TrimSpace(line[len("disallow:"):])
			if path != "" {
				rules.disallow = append(rules.disallow, path)
			}
		}
	}
	return rules, nil
}
