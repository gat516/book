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
	"github.com/jackc/pgx/v5/pgconn"
)

// isDuplicateTargetTerm reports whether err is migration 0016's one-target-per-novel
// index firing. Without this the human-facing bootstrap and correction endpoints would
// answer a duplicate target with a raw 500 rather than the conflict it actually is.
func isDuplicateTargetTerm(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) &&
		pgErr.Code == "23505" &&
		pgErr.ConstraintName == "glossary_novel_target_key"
}

var ErrGlossaryTermNotFound = errors.New("no such glossary term")

// ErrGlossaryTermInvalid is returned when a caller supplies a source_term that cannot
// safely be locked. Declining is the safe outcome: the surface simply gets no locked term,
// which costs a translation constraint rather than correctness.
var ErrGlossaryTermInvalid = errors.New("glossary source term is not a usable surface")

// Kept in lockstep with resolve.py's MIN/MAX_SOURCE_TERM_CHARS. A term one side accepts
// and the other would refuse means the pipeline and the human endpoints disagree about
// what is lockable, which is exactly the divergence this repo ports invariants to avoid.
const (
	minSourceTermChars = 2
	maxSourceTermChars = 80
)

// proseMarkers mirrors resolve.py's _PROSE_MARKERS. An ASCII period is deliberately absent:
// it is legitimate inside a name ("St. Mary") and is not sentence punctuation on its own.
const proseMarkers = "\n\r。！？；：，、"

// sourceTermProblem is the Go port of resolve.py's _source_term_problem — it returns why
// sourceTerm is unusable as a locked surface, or "" if it looks like a name.
//
// Load-bearing, not cosmetic: a locked source term is substituted directly into every later
// chapter before the model sees it (translation.prime_glossary_terms), and CJK has no word
// delimiters, so locking 神 rewrites 精神 into "精God". Counting runes rather than bytes is
// what makes the limits mean the same thing they mean in Python.
func sourceTermProblem(sourceTerm string) string {
	source := strings.TrimSpace(sourceTerm)
	length := len([]rune(source))
	if length < minSourceTermChars {
		return fmt.Sprintf("too short to substitute safely (%d chars)", length)
	}
	if length > maxSourceTermChars {
		return fmt.Sprintf("looks like prose, not a name (%d chars)", length)
	}
	if strings.ContainsAny(source, proseMarkers) {
		return "contains sentence punctuation, so it is prose rather than a name"
	}
	return ""
}

// ErrGlossaryTermConflict is returned by BootstrapGlossaryTerm when the caller supplies a
// (source_term, target_term) pair that collides with an already-locked source_term whose
// target_term differs — the same "already locked, target mismatches" case
// resolve.py's _lock_glossary raises ValueError for.
var ErrGlossaryTermConflict = errors.New("glossary term already locked to a different target")

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
	return s.changeGlossaryTerm(ctx, novelID, sourceTerm, newTarget, atChapter, false)
}

func (s *Store) DeleteGlossaryTerm(ctx context.Context, novelID, sourceTerm string, atChapter int) (int, error) {
	return s.changeGlossaryTerm(ctx, novelID, sourceTerm, "", atChapter, true)
}

func (s *Store) changeGlossaryTerm(ctx context.Context, novelID, sourceTerm, newTarget string, atChapter int, deleted bool) (int, error) {
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
		"SELECT target_term FROM glossary WHERE novel_id = $1 AND source_term = $2 AND NOT deleted FOR UPDATE",
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
		"UPDATE glossary SET target_term = CASE WHEN $5 THEN target_term ELSE $1 END, version = $2, deleted = $5 WHERE novel_id = $3 AND source_term = $4",
		newTarget, newVersion, novelID, sourceTerm, deleted,
	); err != nil {
		if isDuplicateTargetTerm(err) {
			return 0, fmt.Errorf("%w: %q is already the target of another source term",
				ErrGlossaryTermConflict, newTarget)
		}
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

// BootstrapGlossaryTerm seeds a locked glossary term before any chapter has been
// translated (PLAN.md Phase N6): a human supplies (source_term, target_term) pairs
// alongside a paired raw+already-translated bootstrap paste. It takes the exact same
// "original insert" path resolve.py's _lock_glossary itself takes — entity_id starts
// NULL (no entity exists yet) and gets backfilled by _lock_glossary's own NULL-entity_id
// case the first time RESOLVE actually creates the entity for this surface (see that
// function's comment). locked_at_chapter is always 0 here: "locked before any chapter is
// read", never a chapter-specific correction (that's CorrectGlossaryTerm's job).
//
// ON CONFLICT DO NOTHING + a target-match check on conflict, mirroring _lock_glossary
// exactly: re-bootstrapping the same (source_term, target_term) pair is a harmless no-op
// (idempotent, matching this repo's ingestion-is-idempotent principle, §0.7); a
// conflicting target_term for an already-locked source_term is ErrGlossaryTermConflict,
// not a silent overwrite (correcting an existing term is CorrectGlossaryTerm's job, not
// this one's).
func (s *Store) BootstrapGlossaryTerm(ctx context.Context, novelID, sourceTerm, targetTerm string) (int, error) {
	return s.insertGlossaryTerm(ctx, novelID, sourceTerm, targetTerm, 0, "semantic_term")
}

// ConfirmGlossaryTerm publishes a reader-confirmed source-to-display spelling at the
// reader's current knowledge boundary. Unlike bootstrap, it must not backdate the term to
// chapter 0: learning that a named thing exists can itself be a spoiler (§0.2/§0.3).
func (s *Store) ConfirmGlossaryTerm(ctx context.Context, novelID, sourceTerm, targetTerm string, atChapter int, termRole string) (int, error) {
	targetTerm = strings.TrimSpace(targetTerm)
	if targetTerm == "" || len([]rune(targetTerm)) > 160 {
		return 0, errors.New("target_term is required and must be at most 160 characters")
	}
	constraintClass, _, ok := renderingForRole(termRole)
	if !ok {
		return 0, errors.New("term_role must be chinese_person, foreign_person, personal_title, or semantic_term")
	}
	if atChapter < 0 {
		return 0, errors.New("at_chapter must be nonnegative")
	}
	return s.insertGlossaryTerm(ctx, novelID, sourceTerm, targetTerm, atChapter, constraintClass)
}

func (s *Store) insertGlossaryTerm(ctx context.Context, novelID, sourceTerm, targetTerm string, atChapter int, constraintClass string) (int, error) {
	// Shape-check before anything permanent happens, exactly where _lock_glossary does it.
	// A human seeding a term is still seeding one that every later chapter is rewritten
	// against, so "a person typed it" is not on its own a reason to skip the guard.
	if problem := sourceTermProblem(sourceTerm); problem != "" {
		return 0, fmt.Errorf("%w: %s", ErrGlossaryTermInvalid, problem)
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()

	// pg_advisory_xact_lock: same novel-wide serialization CorrectGlossaryTerm and
	// _lock_glossary both use, so a concurrent bootstrap and a concurrent RESOLVE never
	// race on the version counter or the changelog hash chain.
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", novelID); err != nil {
		return 0, err
	}

	var maxVersion int
	if err := tx.QueryRow(ctx,
		"SELECT COALESCE(MAX(version), 0) FROM glossary WHERE novel_id = $1", novelID,
	).Scan(&maxVersion); err != nil {
		return 0, err
	}
	newVersion := maxVersion + 1

	var inserted bool
	err = tx.QueryRow(ctx,
		`INSERT INTO glossary (novel_id, source_term, target_term, version, locked_at_chapter, constraint_class)
		 VALUES ($1, $2, $3, $4, $5, $6)
		 ON CONFLICT (novel_id, source_term) DO UPDATE
		 SET target_term = EXCLUDED.target_term, version = EXCLUDED.version,
		     deleted = false, entity_id = NULL, locked_at_chapter = EXCLUDED.locked_at_chapter,
		     constraint_class = EXCLUDED.constraint_class
		 WHERE glossary.deleted
		 RETURNING true`,
		novelID, sourceTerm, targetTerm, newVersion, atChapter, constraintClass,
	).Scan(&inserted)

	if errors.Is(err, pgx.ErrNoRows) {
		var existingTarget string
		if err := tx.QueryRow(ctx,
			"SELECT target_term, version FROM glossary WHERE novel_id = $1 AND source_term = $2",
			novelID, sourceTerm,
		).Scan(&existingTarget, &newVersion); err != nil {
			return 0, err
		}
		if existingTarget != targetTerm {
			return 0, fmt.Errorf("%w: %q is already locked to %q, not %q",
				ErrGlossaryTermConflict, sourceTerm, existingTarget, targetTerm)
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, err
		}
		return newVersion, nil
	}
	if isDuplicateTargetTerm(err) {
		return 0, fmt.Errorf("%w: %q is already the target of another source term",
			ErrGlossaryTermConflict, targetTerm)
	}
	if err != nil {
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

	// old_target is "" in the hash payload (never a real prior value — this is an
	// original insert, exactly like _lock_glossary's own original-insert path) but NULL
	// in the changelog row itself; that split matches _lock_glossary byte-for-byte.
	payload := pythonJSONArray(novelID, seq, sourceTerm, "", targetTerm, atChapter, prevHash)
	sum := sha256.Sum256([]byte(prevHash + payload))
	rowHash := hex.EncodeToString(sum[:])

	var prevHashArg any
	if prevHash != "" {
		prevHashArg = prevHash
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO glossary_changelog
		   (novel_id, seq, source_term, old_target, new_target, changed_at_chapter, prev_hash, row_hash)
		 VALUES ($1, $2, $3, NULL, $4, $5, $6, $7)`,
		novelID, seq, sourceTerm, targetTerm, atChapter, prevHashArg, rowHash,
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
