package main

import (
	"context"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// httpClient wraps http.Client with the politeness discipline instructions.md §7.2
// requires: a paced, randomized gap between requests + an honest User-Agent + a
// robots.txt check per host, done once here so every Site adapter gets it for free
// instead of re-implementing it.
//
// Pacing is a random wait drawn fresh for each request from [minDelay, maxDelay], timed
// from the END of the previous request to the same host. Two reasons it is a range and
// not a rate: a steady tick is a recognisable machine signature, and a slow response
// under a fixed rate is immediately followed by the next request, which is exactly when
// a struggling site should be asked for less. A site's own robots.txt Crawl-delay
// raises the floor when it asks for more room.
type httpClient struct {
	inner     *http.Client
	minDelay  time.Duration
	maxDelay  time.Duration
	catchUp   time.Duration
	userAgent string

	mu     sync.Mutex
	robots map[string]*robotsRules // host -> parsed rules, fetched lazily and cached
	// lastEnd is when this host's previous response finished, which is what the gap is
	// measured from; reserved is the start time already promised to a request that is
	// waiting, so concurrent callers queue instead of firing together.
	lastEnd  map[string]time.Time
	reserved map[string]time.Time
}

// catchUpContext marks a fetch the reader is waiting on: they have read everything
// stored, so this page is the difference between reading on and stopping. It is paced
// too, just shorter -- one page for a waiting reader is not what makes a scraper rude.
type catchUpKey struct{}

func withCatchUp(ctx context.Context) context.Context {
	return context.WithValue(ctx, catchUpKey{}, true)
}

func isCatchUp(ctx context.Context) bool {
	urgent, _ := ctx.Value(catchUpKey{}).(bool)
	return urgent
}

func newHTTPClient(minDelay, maxDelay, catchUp time.Duration, userAgent string) *httpClient {
	if minDelay <= 0 {
		minDelay = time.Second
	}
	if maxDelay < minDelay {
		maxDelay = minDelay
	}
	if catchUp <= 0 || catchUp > minDelay {
		catchUp = minDelay
	}
	return &httpClient{
		inner:     &http.Client{Timeout: 30 * time.Second},
		minDelay:  minDelay,
		maxDelay:  maxDelay,
		catchUp:   catchUp,
		userAgent: userAgent,
		robots:    make(map[string]*robotsRules),
		lastEnd:   make(map[string]time.Time),
		reserved:  make(map[string]time.Time),
	}
}

// wait blocks until this host may be asked for another page, reserves the slot so
// concurrent jobs on one host queue behind each other rather than firing together, and
// returns the gap it drew so the same value can be re-timed from the response.
func (c *httpClient) wait(ctx context.Context, host string, crawlDelay time.Duration) (time.Duration, error) {
	low, high := c.minDelay, c.maxDelay
	if isCatchUp(ctx) {
		low, high = c.catchUp, 2*c.catchUp
	}
	if crawlDelay > low { // the site asked for more room than our floor
		low = crawlDelay
		if high < low {
			high = low
		}
	}
	gap := low
	if high > low {
		gap += time.Duration(rand.Int64N(int64(high - low)))
	}

	c.mu.Lock()
	// The gap belongs to THIS request and runs from the previous response, so a fetch
	// paced differently from the one before it (catch-up vs normal) waits its own gap
	// rather than inheriting the last one's.
	base := c.lastEnd[host]
	if promised := c.reserved[host]; promised.After(base) {
		base = promised
	}
	start := base.Add(gap)
	if now := time.Now(); start.Before(now) {
		start = now
	}
	c.reserved[host] = start
	c.mu.Unlock()

	if sleep := time.Until(start); sleep > 0 {
		select {
		case <-time.After(sleep):
		case <-ctx.Done():
			return 0, ctx.Err()
		}
	}
	return gap, nil
}

// settle records when this response finished, which is where the next gap starts.
func (c *httpClient) settle(host string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lastEnd[host] = time.Now()
}

// Get performs one polite GET: waits out this host's randomized gap and refuses a path
// robots.txt disallows. Callers are responsible for backoff on error status codes —
// see fetch.go's retry loop, since what counts as "retry" vs. "stop" is adapter-specific
// (a 404 stops a walk, a 429 should back off and retry).
func (c *httpClient) Get(ctx context.Context, rawURL string) (*http.Response, error) {
	return c.get(ctx, rawURL, true)
}

// GetIgnoringRobots applies the same pacing, timeout, and honest user agent as Get while
// skipping only the robots lookup. Keep this explicit at the adapter call
// site so one site's configured exception cannot silently weaken every other adapter.
func (c *httpClient) GetIgnoringRobots(ctx context.Context, rawURL string) (*http.Response, error) {
	return c.get(ctx, rawURL, false)
}

func (c *httpClient) get(ctx context.Context, rawURL string, honorRobots bool) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	origin := req.URL.Scheme + "://" + req.URL.Host
	var crawlDelay time.Duration
	if honorRobots {
		allowed, delay, err := c.robotsFor(ctx, origin, req.URL.Path)
		if err != nil {
			// A robots.txt fetch failure shouldn't block scraping (many sites have none) —
			// fail open on the check itself, not on the site's actual content.
			allowed = true
		}
		if !allowed {
			return nil, fmt.Errorf("robots.txt disallows %s", rawURL)
		}
		crawlDelay = delay
	}

	if _, err := c.wait(ctx, req.URL.Host, crawlDelay); err != nil {
		return nil, err
	}

	req.Header.Set("User-Agent", c.userAgent)
	resp, err := c.inner.Do(req)
	// Whatever the outcome, the next gap starts now, so a slow response pushes the next
	// request back instead of being followed straight away -- and a site that just timed
	// out on us is the last one to hammer.
	c.settle(req.URL.Host)
	return resp, err
}

type robotsRules struct {
	disallow []string // path prefixes disallowed for User-agent: * (the only group we honor)
	// crawlDelay is the site's requested seconds between requests, 0 when it asks for none.
	crawlDelay time.Duration
}

// robotsFor answers both questions one robots.txt fetch can answer: may this path be
// fetched, and how much room does the site ask for between requests.
func (c *httpClient) robotsFor(ctx context.Context, origin, path string) (bool, time.Duration, error) {
	rules, err := c.rulesFor(ctx, origin)
	if err != nil {
		return true, 0, err
	}
	for _, prefix := range rules.disallow {
		if prefix != "" && strings.HasPrefix(path, prefix) {
			return false, rules.crawlDelay, nil
		}
	}
	return true, rules.crawlDelay, nil
}

// robotsAllow is the preview's view of the same check (preview.go).
func (c *httpClient) robotsAllow(ctx context.Context, origin, path string) (bool, error) {
	allowed, _, err := c.robotsFor(ctx, origin, path)
	return allowed, err
}

func (c *httpClient) rulesFor(ctx context.Context, origin string) (*robotsRules, error) {
	c.mu.Lock()
	rules, cached := c.robots[origin]
	c.mu.Unlock()
	if cached {
		return rules, nil
	}
	rules, err := fetchRobots(ctx, c.inner, origin, c.userAgent)
	if err != nil {
		return &robotsRules{}, err
	}
	c.mu.Lock()
	c.robots[origin] = rules
	c.mu.Unlock()
	return rules, nil
}

// fetchRobots parses only what this scraper needs from the User-agent: * group: Disallow
// paths and Crawl-delay. Not a general robots.txt implementation (no per-bot groups) —
// sufficient for "don't fetch a path the site closed off, and wait as long as it asks."
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
		case applies && strings.HasPrefix(strings.ToLower(line), "crawl-delay:"):
			if seconds, err := strconv.ParseFloat(strings.TrimSpace(line[len("crawl-delay:"):]), 64); err == nil && seconds > 0 {
				rules.crawlDelay = time.Duration(seconds * float64(time.Second))
			}
		}
	}
	return rules, nil
}
