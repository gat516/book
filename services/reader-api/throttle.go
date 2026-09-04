package main

// Failed-attempt throttling for the operator credential.
//
// One shared secret authorizes quarantining a book's knowledge and replacing it, and until
// now it could be guessed at line speed with no record that anyone tried: there is no rate
// limiting anywhere in this service or in ingest-api, and neither requireOperator nor
// requireInternalToken logs a failure. This is the first of both, kept deliberately small.
//
// Two decisions worth knowing about before changing anything here:
//
//   - A request with NO operator header is not an attempt. Every reader polling
//     GET /novels/{id}/repair sends no token, and counting those as failures would let
//     ordinary reader traffic lock the operator out of their own repair controls. Only a
//     header that is present and wrong counts.
//
//   - The key is the client IP from r.RemoteAddr, never X-Forwarded-For. Nothing in this
//     repo reads that header, and trusting an unverified one would let an attacker rotate
//     the limiter key for free — worse than no limiter, because it would look like one.
//     Behind a reverse proxy every request shares the proxy's address and this degrades to
//     a single global limiter. That is an acceptable failure mode: it still bounds guessing,
//     and the block always expires, so a legitimate operator is delayed rather than locked
//     out permanently.

import (
	"fmt"
	"log"
	"net"
	"net/http"
	"sync"
	"time"
)

const (
	// Attempts allowed before delays begin. A person fumbling a paste should not be
	// punished; a script should.
	throttleFreeAttempts = 3
	throttleBaseDelay    = time.Second
	throttleMaxDelay     = 5 * time.Minute
	// Bounds memory: the key space is attacker-influenced, so it cannot grow freely.
	throttleMaxKeys = 4096
	// Records idle this long are dropped, so a one-off typo does not occupy a slot.
	throttleIdleTTL = 30 * time.Minute
)

type attemptRecord struct {
	failures     int
	blockedUntil time.Time
	lastSeen     time.Time
}

type authThrottle struct {
	mu      sync.Mutex
	records map[string]*attemptRecord
	// now is injectable so the tests do not have to sleep through a backoff.
	now func() time.Time
}

func newAuthThrottle() *authThrottle {
	return &authThrottle{records: map[string]*attemptRecord{}, now: time.Now}
}

// clientKey identifies the caller for throttling. Falls back to the raw RemoteAddr when it
// carries no port, and to a single shared bucket when there is nothing at all — a shared
// bucket is a weaker limit but never a disabled one.
func clientKey(r *http.Request) string {
	if r.RemoteAddr == "" {
		return "unknown"
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// blockedFor reports how long this caller must wait, or zero if it may attempt now.
func (t *authThrottle) blockedFor(key string) time.Duration {
	t.mu.Lock()
	defer t.mu.Unlock()
	record, ok := t.records[key]
	if !ok {
		return 0
	}
	remaining := record.blockedUntil.Sub(t.now())
	if remaining <= 0 {
		return 0
	}
	return remaining
}

// recordFailure counts one wrong token and extends this caller's backoff.
func (t *authThrottle) recordFailure(key string) time.Duration {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	record, ok := t.records[key]
	if !ok {
		t.evictLocked(now)
		record = &attemptRecord{}
		t.records[key] = record
	}
	record.failures++
	record.lastSeen = now

	delay := time.Duration(0)
	if record.failures > throttleFreeAttempts {
		delay = throttleBaseDelay << (record.failures - throttleFreeAttempts - 1)
		if delay > throttleMaxDelay || delay <= 0 { // <=0 guards the shift overflowing
			delay = throttleMaxDelay
		}
		record.blockedUntil = now.Add(delay)
	}
	// The value is never logged — only that a wrong one arrived, from where, and how often.
	log.Printf("operator auth: rejected attempt %d from %s (delay %s)", record.failures, key, delay)
	return delay
}

// recordSuccess clears a caller's history, so a legitimate operator who mistyped once is
// not still paying for it.
func (t *authThrottle) recordSuccess(key string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.records, key)
}

// evictLocked keeps the map bounded. Callers hold t.mu.
func (t *authThrottle) evictLocked(now time.Time) {
	if len(t.records) < throttleMaxKeys {
		return
	}
	oldestKey, oldest := "", now
	for key, record := range t.records {
		if now.Sub(record.lastSeen) > throttleIdleTTL {
			delete(t.records, key)
			continue
		}
		if record.lastSeen.Before(oldest) {
			oldestKey, oldest = key, record.lastSeen
		}
	}
	// Still full after dropping idle records: give up the least recently active one.
	if len(t.records) >= throttleMaxKeys && oldestKey != "" {
		delete(t.records, oldestKey)
	}
}

// minRepairOperatorToken is the shortest token reader-api will start with. Short enough not
// to be annoying, long enough that the throttle above is a second line of defence rather
// than the only one.
const minRepairOperatorToken = 32

// validateOperatorToken decides whether reader-api may start with this configuration.
//
// Empty is valid and means repair writes are disabled. Set-but-weak is a misconfiguration:
// the token authorizes quarantining a book's knowledge, so refuse to start rather than
// offer that behind a guessable secret. The cost is deliberate — a deployment with a short
// token stops booting until it is changed.
func validateOperatorToken(token string) error {
	if token == "" {
		return nil
	}
	if len(token) < minRepairOperatorToken {
		return fmt.Errorf(
			"READER_REPAIR_OPERATOR_TOKEN must be at least %d characters (got %d); "+
				"leave it unset to disable repair controls", minRepairOperatorToken, len(token))
	}
	return nil
}
