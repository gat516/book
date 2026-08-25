package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

var ErrGlossaryTermNotFound = errors.New("no such glossary term")

// CorrectGlossaryTerm updates a locked glossary term's target and appends a
// tamper-evident audit row, reusing the exact invariants
// services/pipeline/pipeline/stages/resolve.py's _lock_glossary established (read there
// in full before touching this — two things are NOT obvious from a skim and are exactly
// where a port can silently diverge):
//
//  1. version is a NOVEL-WIDE monotonic counter (MAX(version) across every term in the
//     novel), not a per-term version. _lock_glossary's version query has no source_term
//     filter; matching that here means computing new_version from MAX(version) across
//     the whole novel, not this row's own version + 1.
//  2. The changelog hash payload is a JSON array
//     [novel_id, seq, source_term, old_target, new_target, chapter, prev_hash],
//     hashed as sha256(prev_hash + payload_json). old_target is "" (not the actual
//     value) on the ORIGINAL insert path (_lock_glossary never updates, so it has no
//     real old value) — a correction always has a real one, so it belongs here for real.
//     The JSON encoding must byte-match Python's json.dumps(..., ensure_ascii=False,
//     separators=(",", ":")): compact (no spaces), non-ASCII emitted literally. Go's
//     encoding/json additionally HTML-escapes '<', '>', '&' by default and unconditionally
//     escapes U+2028/U+2029 — neither of which Python's json.dumps does — so this builds
//     the array by hand (pythonJSONString below) rather than trusting json.Marshal, to
//     guarantee the two languages hash identically. See glossary_hash_test.go for the
//     cross-language guard.
func (s *Store) CorrectGlossaryTerm(ctx context.Context, novelID, sourceTerm, newTarget string, atChapter int) (int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()

	// pg_advisory_xact_lock is transaction-scoped: it only serializes concurrent
	// glossary writes for this novel_id if held inside an explicit transaction, exactly
	// like _lock_glossary's own contract.
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return 0, err
	}

	var oldTarget string
	err = tx.QueryRow(ctx,
		"SELECT target_term FROM glossary WHERE novel_id = $1 AND source_term = $2 FOR UPDATE",
		novelID, sourceTerm,
	).Scan(&oldTarget)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, ErrGlossaryTermNotFound
	}
	if err != nil {
		return 0, err
	}

	var maxVersion int
	if err := tx.QueryRow(ctx,
		"SELECT COALESCE(MAX(version), 0) FROM glossary WHERE novel_id = $1", novelID,
	).Scan(&maxVersion); err != nil {
		return 0, err
	}
	newVersion := maxVersion + 1

	if _, err := tx.Exec(ctx,
		"UPDATE glossary SET target_term = $1, version = $2 WHERE novel_id = $3 AND source_term = $4",
		newTarget, newVersion, novelID, sourceTerm,
	); err != nil {
		return 0, err
	}

	var prevSeq int
	var prevHash string
	err = tx.QueryRow(ctx,
		"SELECT seq, row_hash FROM glossary_changelog WHERE novel_id = $1 ORDER BY seq DESC LIMIT 1",
		novelID,
	).Scan(&prevSeq, &prevHash)
	if errors.Is(err, pgx.ErrNoRows) {
		prevSeq, prevHash = 0, ""
	} else if err != nil {
		return 0, err
	}
	seq := prevSeq + 1

	payload := pythonJSONArray(novelID, seq, sourceTerm, oldTarget, newTarget, atChapter, prevHash)
	sum := sha256.Sum256([]byte(prevHash + payload))
	rowHash := hex.EncodeToString(sum[:])

	var prevHashArg any
	if prevHash != "" {
		prevHashArg = prevHash
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO glossary_changelog
		   (novel_id, seq, source_term, old_target, new_target, changed_at_chapter, prev_hash, row_hash)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		novelID, seq, sourceTerm, oldTarget, newTarget, atChapter, prevHashArg, rowHash,
	); err != nil {
		return 0, err
	}

	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return newVersion, nil
}

// pythonJSONArray builds the exact JSON array string
// json.dumps([novelID, seq, sourceTerm, oldTarget, newTarget, chapter, prevHash],
// ensure_ascii=False, separators=(",", ":")) would produce in Python.
func pythonJSONArray(novelID string, seq int, sourceTerm, oldTarget, newTarget string, chapter int, prevHash string) string {
	fields := []string{
		pythonJSONString(novelID),
		strconv.Itoa(seq),
		pythonJSONString(sourceTerm),
		pythonJSONString(oldTarget),
		pythonJSONString(newTarget),
		strconv.Itoa(chapter),
		pythonJSONString(prevHash),
	}
	return "[" + strings.Join(fields, ",") + "]"
}

// pythonJSONString escapes s exactly as Python's json.dumps(s, ensure_ascii=False) does:
// '"' and '\' get backslash-escaped, the standard short escapes cover \b \f \n \r \t,
// every other control character (< 0x20) becomes \u00XX, and everything else — including
// non-ASCII Unicode, and '<' '>' '&' which Go's encoding/json would otherwise
// HTML-escape — is emitted literally.
func pythonJSONString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}
