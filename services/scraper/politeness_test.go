package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Pacing is the whole politeness story for a site that allows crawling, so it is checked
// against a real server rather than by reading the fields back.
func TestRequestsToOneHostAreSpacedAndRandomized(t *testing.T) {
	var times []time.Time
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		times = append(times, time.Now())
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := newHTTPClient(40*time.Millisecond, 120*time.Millisecond, 10*time.Millisecond, "test-agent")
	gaps := make([]time.Duration, 0, 3)
	for i := 0; i < 4; i++ {
		resp, err := client.GetIgnoringRobots(context.Background(), server.URL+"/c/1")
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		resp.Body.Close()
		if i > 0 {
			gaps = append(gaps, times[i].Sub(times[i-1]))
		}
	}
	for i, gap := range gaps {
		if gap < 40*time.Millisecond {
			t.Errorf("gap %d = %v, want at least the 40ms floor", i, gap)
		}
	}
	// Drawn per request: identical gaps would mean a fixed tick, the pattern this avoids.
	if gaps[0] == gaps[1] && gaps[1] == gaps[2] {
		t.Errorf("gaps are not randomized: %v", gaps)
	}
}

func TestCrawlDelayRaisesTheFloorAndDisallowStillStops(t *testing.T) {
	var robotsHits int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			robotsHits++
			_, _ = w.Write([]byte("User-agent: *\nCrawl-delay: 0.2\nDisallow: /private\n"))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := newHTTPClient(time.Millisecond, 2*time.Millisecond, time.Millisecond, "test-agent")
	start := time.Now()
	for i := 0; i < 2; i++ {
		resp, err := client.Get(context.Background(), server.URL+"/c/1")
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		resp.Body.Close()
	}
	// Two requests, one 200ms crawl-delay between them, even though our own floor is 1ms.
	if elapsed := time.Since(start); elapsed < 200*time.Millisecond {
		t.Errorf("crawl-delay ignored: two requests took %v", elapsed)
	}
	if _, err := client.Get(context.Background(), server.URL+"/private/x"); err == nil ||
		!strings.Contains(err.Error(), "robots.txt disallows") {
		t.Errorf("disallowed path fetched anyway: %v", err)
	}
	if robotsHits != 1 {
		t.Errorf("robots.txt fetched %d times, want once per host", robotsHits)
	}
}

// A reader who has read everything stored is waiting on the next page, so that one fetch
// uses the short catch-up pace instead of the full gap.
func TestCatchUpFetchesUseTheShortPace(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := newHTTPClient(2*time.Second, 4*time.Second, 20*time.Millisecond, "test-agent")
	for i := 0; i < 2; i++ { // the first request never waits; the second shows the pace
		resp, err := client.GetIgnoringRobots(withCatchUp(context.Background()), server.URL+"/c/1")
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		resp.Body.Close()
	}
	start := time.Now()
	resp, err := client.GetIgnoringRobots(context.Background(), server.URL+"/c/2")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	// Back to the normal pace once the buffer is being built again.
	if waited := time.Since(start); waited < time.Second {
		t.Errorf("ordinary fetch waited %v, want the full gap", waited)
	}
}
