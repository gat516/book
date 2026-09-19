package main

// FACTS build controls: the operator surface for a book's story facts. FACTS reads only
// its own chapter, so every action here is per chapter -- there is no generation to
// rebuild and no chapter order to wait on. A chapter is done once chapter.facts_count is
// set (0110); facts themselves are never edited here (§0.2).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strconv"

	"github.com/jackc/pgx/v5"
)

const factsAdvisoryLockSQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

// FactsStatus is deliberately metadata-only: counts and whether work is in flight, never
// fact text.
type FactsStatus struct {
	NovelID          string `json:"novel_id"`
	EligibleChapters int    `json:"eligible_chapters"`
	DoneChapters     int    `json:"done_chapters"`
	MissingChapters  int    `json:"missing_chapters"`
	// Running is true while facts work for this book is queued, claimed, or waiting on a
	// scheduled retry. MissingChapters alone cannot drive a Find/Pause toggle: a paused
	// book, a never-started book and a failed chapter all have missing chapters and no
	// work coming.
	Running bool `json:"running"`
}

// lockFactsNovel serializes the controls for one book and confirms the novel exists.
func lockFactsNovel(ctx context.Context, tx pgx.Tx, novelID string) error {
	if _, err := tx.Exec(ctx, factsAdvisoryLockSQL, "facts:"+novelID); err != nil {
		return err
	}
	var exists bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM novel WHERE id=$1)", novelID).Scan(&exists); err != nil {
		return fmt.Errorf("find novel: %w", err)
	}
	if !exists {
		return pgx.ErrNoRows
	}
	return nil
}

// resumeChapterSQL clears a chapter's pause and any scheduled retry, so an explicit
// action replaces the automatic one rather than racing it.
const resumeChapterSQL = `enrichment_discarded=false,
	enrichment_retry_at=NULL,enrichment_attempts=0,
	provider_retry_at=NULL,provider_retry_attempts=0,provider_retry_category=NULL`

// pauseChapterSQL is resumeChapterSQL's inverse: paused, with nothing scheduled.
const pauseChapterSQL = `enrichment_discarded=true,
	enrichment_retry_at=NULL,enrichment_attempts=0,
	provider_retry_at=NULL,provider_retry_attempts=0,provider_retry_category=NULL`

// retryFacts re-runs one chapter's facts now, as an operator retry (priority). A failure
// of this attempt does not reschedule (worker.py): the operator decides what happens next.
func (s *Store) retryFacts(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin facts retry: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockFactsNovel(ctx, tx, novelID); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE chapter SET `+resumeChapterSQL+`
		WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter); err != nil {
		return fmt.Errorf("resume chapter: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit facts retry: %w", err)
	}
	return s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: chapter, Priority: true, Enrichment: true})
}

// discardChapterFacts stops one chapter's facts attempt. The durable flag fences an
// in-flight worker at its next stage boundary; removing the queued pointer handles work
// that has not been claimed yet. Retry clears the flag and starts a fresh attempt.
func (s *Store) discardChapterFacts(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin chapter discard: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockFactsNovel(ctx, tx, novelID); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE chapter SET `+pauseChapterSQL+`
		WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter); err != nil {
		return fmt.Errorf("mark chapter discarded: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit chapter discard: %w", err)
	}
	return s.removeQueuedChapter(ctx, novelID, chapter)
}

func (s *Store) removeQueuedChapter(ctx context.Context, novelID string, chapter int) error {
	entries, err := s.redis.LRange(ctx, pendingQueue, 0, -1).Result()
	if err != nil {
		return fmt.Errorf("list pending chapter work: %w", err)
	}
	for _, raw := range entries {
		var msg QueueMessage
		if json.Unmarshal([]byte(raw), &msg) != nil || msg.NovelID != novelID || msg.ChapterIndex != chapter {
			continue
		}
		if err := s.redis.LRem(ctx, pendingQueue, 0, raw).Err(); err != nil {
			return fmt.Errorf("remove pending chapter work: %w", err)
		}
	}
	return nil
}

// stopFacts pauses the whole book's facts work: the novel-wide form of
// discardChapterFacts. It is a pause, not deletion (§0): a chapter that already has its
// facts is finished rather than in flight, so it is left alone.
func (s *Store) stopFacts(ctx context.Context, novelID string) (int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin facts stop: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockFactsNovel(ctx, tx, novelID); err != nil {
		return 0, err
	}
	tag, err := tx.Exec(ctx, `UPDATE chapter AS c SET `+pauseChapterSQL+`
		WHERE c.novel_id=$1 AND NOT c.enrichment_discarded AND c.facts_count IS NULL`, novelID)
	if err != nil {
		return 0, fmt.Errorf("mark facts stopped: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit facts stop: %w", err)
	}
	return int(tag.RowsAffected()), s.removeQueuedEnrichment(ctx, novelID)
}

// extractFacts finds the book's missing facts: every readable chapter without them is
// resumed (a stop or per-chapter discard lifted, any automatic retry replaced) and
// enqueued in chapter order. Chapters that already have facts are untouched.
//
// A chapter with a pointer already pending or claimed is skipped entirely: enqueueing it
// again would run it twice.
func (s *Store) extractFacts(ctx context.Context, novelID string) (int, error) {
	queued, err := s.queuedChapters(ctx, novelID, false)
	if err != nil {
		return 0, err
	}
	busy := make([]int, 0, len(queued))
	for index := range queued {
		busy = append(busy, index)
	}
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin facts extract: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockFactsNovel(ctx, tx, novelID); err != nil {
		return 0, err
	}
	rows, err := tx.Query(ctx, `UPDATE chapter AS c SET `+resumeChapterSQL+`
		WHERE c.novel_id=$1 AND (c.translation_ready OR c.status='done')
		  AND NOT (c.chapter_index = ANY($2)) AND c.facts_count IS NULL
		RETURNING c.chapter_index`, novelID, busy)
	if err != nil {
		return 0, fmt.Errorf("resume unfinished chapters: %w", err)
	}
	chapters, err := pgx.CollectRows(rows, pgx.RowTo[int])
	if err != nil {
		return 0, fmt.Errorf("resume unfinished chapters: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit facts extract: %w", err)
	}
	sort.Ints(chapters)
	for _, index := range chapters {
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index, Enrichment: true}); err != nil {
			return 0, err
		}
	}
	return len(chapters), nil
}

// queuedChapters reports which of novelID's chapters have a pointer pending or claimed.
// enrichmentOnly narrows it to facts work; otherwise a translation pointer counts too,
// since the worker hands a translated chapter straight on to enrichment.
func (s *Store) queuedChapters(ctx context.Context, novelID string, enrichmentOnly bool) (map[int]bool, error) {
	out := map[int]bool{}
	for _, key := range []string{pendingQueue, "jobs:processing"} {
		entries, err := s.redis.LRange(ctx, key, 0, -1).Result()
		if err != nil {
			return nil, fmt.Errorf("list %s: %w", key, err)
		}
		for _, raw := range entries {
			var msg QueueMessage
			if json.Unmarshal([]byte(raw), &msg) != nil || msg.NovelID != novelID {
				continue
			}
			if enrichmentOnly && !msg.Enrichment {
				continue
			}
			out[msg.ChapterIndex] = true
		}
	}
	return out, nil
}

// removeQueuedEnrichment drops the novel's queued facts work only. Translation shares
// this queue, and stopping facts must never cancel a chapter's prose: an unreadable
// chapter is a strictly worse outcome than one without facts (§0).
func (s *Store) removeQueuedEnrichment(ctx context.Context, novelID string) error {
	entries, err := s.redis.LRange(ctx, pendingQueue, 0, -1).Result()
	if err != nil {
		return fmt.Errorf("list pending chapter work: %w", err)
	}
	for _, raw := range entries {
		var msg QueueMessage
		if json.Unmarshal([]byte(raw), &msg) != nil || msg.NovelID != novelID || !msg.Enrichment {
			continue
		}
		if err := s.redis.LRem(ctx, pendingQueue, 0, raw).Err(); err != nil {
			return fmt.Errorf("remove pending chapter work: %w", err)
		}
	}
	return nil
}

func (s *Store) factsStatus(ctx context.Context, novelID string) (FactsStatus, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return FactsStatus{}, fmt.Errorf("begin facts status: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockFactsNovel(ctx, tx, novelID); err != nil {
		return FactsStatus{}, err
	}
	out := FactsStatus{NovelID: novelID}
	// A chapter is eligible once its translation is readable, done once it has facts.
	if err = tx.QueryRow(ctx, `SELECT count(*)::int,
		       count(*) FILTER (WHERE facts_count IS NOT NULL)::int,
		       count(*) FILTER (WHERE facts_count IS NULL)::int
		  FROM chapter WHERE novel_id=$1 AND (translation_ready OR status='done')`, novelID).
		Scan(&out.EligibleChapters, &out.DoneChapters, &out.MissingChapters); err != nil {
		return FactsStatus{}, fmt.Errorf("count facts chapters: %w", err)
	}
	if err = tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM chapter
		WHERE novel_id=$1 AND NOT enrichment_discarded
		  AND (enrichment_retry_at IS NOT NULL OR provider_retry_at IS NOT NULL))`,
		novelID).Scan(&out.Running); err != nil {
		return FactsStatus{}, fmt.Errorf("check scheduled facts retries: %w", err)
	}
	if !out.Running {
		queued, qerr := s.queuedChapters(ctx, novelID, true)
		if qerr != nil {
			return FactsStatus{}, qerr
		}
		out.Running = len(queued) > 0
	}
	if err = tx.Commit(ctx); err != nil {
		return FactsStatus{}, fmt.Errorf("commit facts status: %w", err)
	}
	return out, nil
}

func (a *API) factsRetry(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.retryFacts(r.Context(), r.PathValue("id"), chapter)
	a.writeFactsAction(w, "retry", map[string]any{"retried": true, "chapter_index": chapter}, err)
}

func (a *API) factsDiscardChapter(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.discardChapterFacts(r.Context(), r.PathValue("id"), chapter)
	a.writeFactsAction(w, "discard", map[string]any{"discarded": true, "chapter_index": chapter}, err)
}

func (a *API) factsStop(w http.ResponseWriter, r *http.Request) {
	stopped, err := a.store.stopFacts(r.Context(), r.PathValue("id"))
	a.writeFactsAction(w, "stop", map[string]any{"stopped": true, "chapters_stopped": stopped}, err)
}

func (a *API) factsExtract(w http.ResponseWriter, r *http.Request) {
	chapters, err := a.store.extractFacts(r.Context(), r.PathValue("id"))
	a.writeFactsAction(w, "extract", map[string]any{"chapters_enqueued": chapters}, err)
}

func (a *API) getFactsStatus(w http.ResponseWriter, r *http.Request) {
	status, err := a.store.factsStatus(r.Context(), r.PathValue("id"))
	a.writeFactsAction(w, "status", status, err)
}

func (a *API) writeFactsAction(w http.ResponseWriter, action string, body any, err error) {
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case err != nil:
		log.Printf("facts %s: %v", action, err)
		writeErr(w, http.StatusInternalServerError, "facts action failed")
	default:
		writeJSON(w, http.StatusOK, body)
	}
}
