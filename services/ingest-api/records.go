package main

// Records maintenance actions: the operator surface that replaced repair and chapter
// re-extraction. Retries re-run work in place; a rebuild starts a new generation and
// re-enriches chronologically, because published extraction content is immutable
// (§0.2) and a prompt/ontology/model change is a different generation, not an edit.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

var (
	ErrRecordsRebuildActive   = errors.New("a records rebuild is already active")
	ErrRecordsRebuildConflict = errors.New("records rebuild cannot be discarded")
	// ErrRecordsOutOfOrder refuses a chapter retry that the worker's generation fence
	// would reject anyway ("earlier records are unpublished"): failing here is immediate
	// and says why, instead of queueing an attempt that is certain to fail.
	ErrRecordsOutOfOrder = errors.New("an earlier chapter has not been extracted yet")
	ErrRecordsReviewInvalid   = errors.New("invalid records review request")
	ErrRecordsReviewConflict  = errors.New("records review request conflicts with an existing decision")
	ErrRecordsReviewNotFound  = errors.New("record is not available for review")
	ErrRecordsReviewStale     = errors.New("record review is stale; reload before reviewing")
)

const recordsAdvisoryLockSQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

// RecordsRebuildStatus is deliberately metadata-only: it lets an operator recover the
// generation id after a reload without exposing chapter text or knowledge rows.
type RecordsRebuildStatus struct {
	NovelID                 string  `json:"novel_id"`
	ActiveGenerationID      *string `json:"active_generation_id"`
	ActiveState             string  `json:"active_state"`
	PredecessorGenerationID *string `json:"predecessor_generation_id"`
	HasPredecessor          bool    `json:"has_predecessor"`
	EligibleChapters        int     `json:"eligible_chapters"`
	PublishedChapters       int     `json:"published_chapters"`
	MissingChapters         int     `json:"missing_chapters"`
	Discardable             bool    `json:"discardable"`
	// Running is true while graph work for this book is queued, claimed, or waiting on a
	// scheduled retry. MissingChapters alone cannot drive an Extract/Stop toggle: a
	// stopped build, a never-started book and a failed chapter all have missing chapters
	// and no work coming.
	Running bool `json:"running"`
}

func lockRecordsNovel(ctx context.Context, tx pgx.Tx, novelID string) error {
	_, err := tx.Exec(ctx, recordsAdvisoryLockSQL, "records:"+novelID)
	return err
}

// recordsRebuildCounts is the single completion predicate shared by rebuild admission,
// discard, and the reload-safe status endpoint. A saved chapter is eligible once its
// translation is readable; a replacement is complete only when every eligible chapter
// has a published run. Empty eligible sets remain discardable while work has not begun.
func recordsRebuildCounts(ctx context.Context, tx pgx.Tx, novelID, generation string) (eligible, published, missing int, err error) {
	err = tx.QueryRow(ctx, `WITH eligible AS (
		SELECT c.chapter_index
		  FROM chapter c
		 WHERE c.novel_id=$1 AND (c.translation_ready OR c.status='done')
	), coverage AS (
		SELECT e.chapter_index, EXISTS (
			SELECT 1 FROM record_run r
			 WHERE r.novel_id=$1 AND r.generation_id=$2
			   AND r.chapter_index=e.chapter_index AND r.status='published'
		) AS covered FROM eligible e
	)
	SELECT count(*)::int,
	       count(*) FILTER (WHERE covered)::int,
	       count(*) FILTER (WHERE NOT covered)::int
	  FROM coverage`, novelID, generation).Scan(&eligible, &published, &missing)
	return
}

// retryRecords clears one chapter's failed run so the worker re-runs it. Published runs
// are left alone: rebuilding already-published extraction is a generation-level action.
func (s *Store) retryRecords(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin records retry: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	// Serialize against a publishing worker and against a concurrent reset.
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return fmt.Errorf("lock records retry: %w", err)
	}
	var generation string
	if err = tx.QueryRow(ctx,
		"SELECT active_record_generation::text FROM novel WHERE id=$1 FOR UPDATE", novelID,
	).Scan(&generation); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return pgx.ErrNoRows
		}
		return fmt.Errorf("find novel: %w", err)
	}
	var waitingOn *int
	if err = tx.QueryRow(ctx, `SELECT min(c.chapter_index) FROM chapter c
		WHERE c.novel_id=$1 AND c.chapter_index < $2
		  AND c.translation_ready
		  AND NOT EXISTS (SELECT 1 FROM record_run r
		        WHERE r.novel_id=c.novel_id AND r.generation_id=$3
		          AND r.chapter_index=c.chapter_index AND r.status='published')`,
		novelID, chapter, generation).Scan(&waitingOn); err != nil {
		return fmt.Errorf("check earlier chapters: %w", err)
	}
	if waitingOn != nil {
		return fmt.Errorf("%w: chapter %d", ErrRecordsOutOfOrder, *waitingOn)
	}
	// The explicit retry replaces any automatic one: clear the schedule here rather than
	// when the worker claims the pointer, so there is never a window where the chapter
	// carries both. A failure of this attempt does not reschedule (worker.py).
	if _, err = tx.Exec(ctx, `UPDATE chapter SET enrichment_discarded=false,
		enrichment_retry_at=NULL,enrichment_retry_generation_id=NULL,enrichment_attempts=0,
		provider_retry_at=NULL,provider_retry_generation_id=NULL,provider_retry_attempts=0,
		provider_retry_category=NULL
		WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter); err != nil {
		return fmt.Errorf("resume discarded chapter: %w", err)
	}
	if _, err = tx.Exec(ctx,
		"DELETE FROM record_run WHERE novel_id=$1 AND generation_id=$2 AND chapter_index=$3 AND status<>'published'",
		novelID, generation, chapter); err != nil {
		return fmt.Errorf("clear failed run: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit records retry: %w", err)
	}
	return s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: chapter, Priority: true, Enrichment: true, RecordGenerationID: generation})
}

// discardChapterEnrichment stops one graph attempt without touching published knowledge.
// The durable flag fences an in-flight worker at its next stage boundary; removing the
// queued pointer handles work that has not been claimed yet. Retry explicitly clears the
// flag and starts a fresh attempt.
func (s *Store) discardChapterEnrichment(ctx context.Context, novelID string, chapter int) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin chapter discard: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return fmt.Errorf("lock chapter discard: %w", err)
	}
	var generation string
	if err = tx.QueryRow(ctx, `SELECT active_record_generation::text FROM novel WHERE id=$1 FOR UPDATE`, novelID).Scan(&generation); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return pgx.ErrNoRows
		}
		return fmt.Errorf("find active generation: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE chapter SET enrichment_discarded=true,
		enrichment_retry_at=NULL,enrichment_retry_generation_id=NULL,enrichment_attempts=0,
		provider_retry_at=NULL,provider_retry_generation_id=NULL,provider_retry_attempts=0,
		provider_retry_category=NULL
		WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter); err != nil {
		return fmt.Errorf("mark chapter discarded: %w", err)
	}
	if _, err = tx.Exec(ctx, `DELETE FROM record_run
		WHERE novel_id=$1 AND generation_id=$2 AND chapter_index=$3 AND status <> 'published'`, novelID, generation, chapter); err != nil {
		return fmt.Errorf("clear discarded run: %w", err)
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

// stopRecordsBuild pauses the whole book's graph build: the novel-wide form of
// discardChapterEnrichment, for the case the per-chapter button cannot reach -- a first
// build (no predecessor to roll back to, so discardRecordsRebuild refuses it) with work
// queued across every chapter. It is a pause, not deletion (§0): a chapter already
// published in the active generation is finished rather than in flight, so it is left
// alone and no knowledge a reader has been served can be lost here.
func (s *Store) stopRecordsBuild(ctx context.Context, novelID string) (int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin records stop: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return 0, fmt.Errorf("lock records stop: %w", err)
	}
	var generation string
	if err = tx.QueryRow(ctx,
		"SELECT active_record_generation::text FROM novel WHERE id=$1 FOR UPDATE", novelID,
	).Scan(&generation); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, pgx.ErrNoRows
		}
		return 0, fmt.Errorf("find active generation: %w", err)
	}
	tag, err := tx.Exec(ctx, `UPDATE chapter AS c SET enrichment_discarded=true,
		enrichment_retry_at=NULL,enrichment_retry_generation_id=NULL,enrichment_attempts=0,
		provider_retry_at=NULL,provider_retry_generation_id=NULL,provider_retry_attempts=0,
		provider_retry_category=NULL
		WHERE c.novel_id=$1 AND NOT c.enrichment_discarded
		  AND NOT EXISTS (SELECT 1 FROM record_run r
		        WHERE r.novel_id=c.novel_id AND r.generation_id=$2
		          AND r.chapter_index=c.chapter_index AND r.status='published')`, novelID, generation)
	if err != nil {
		return 0, fmt.Errorf("mark build stopped: %w", err)
	}
	if _, err = tx.Exec(ctx,
		"DELETE FROM record_run WHERE novel_id=$1 AND generation_id=$2 AND status<>'published'",
		novelID, generation); err != nil {
		return 0, fmt.Errorf("clear unfinished runs: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit records stop: %w", err)
	}
	return int(tag.RowsAffected()), s.removeQueuedEnrichment(ctx, novelID)
}

// extractRecords continues the book's graph in its active generation: every readable
// chapter without a published run is resumed (a stop or per-chapter discard lifted, a
// failed run cleared, any automatic retry replaced) and enqueued in chapter order.
// Published chapters are untouched. This is the everyday "Extract facts" action; a new
// generation is only for rebuilding from scratch (rebuildRecords), which re-extracts
// chapters that are already done.
//
// A chapter with a pointer already pending or claimed is skipped entirely: enqueueing it
// again would run it twice, and clearing its run row would pull state out from under a
// worker that is mid-chapter.
func (s *Store) extractRecords(ctx context.Context, novelID string) (int, error) {
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
		return 0, fmt.Errorf("begin records extract: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return 0, fmt.Errorf("lock records extract: %w", err)
	}
	var generation *string
	if err = tx.QueryRow(ctx,
		"SELECT active_record_generation::text FROM novel WHERE id=$1 FOR UPDATE", novelID,
	).Scan(&generation); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, pgx.ErrNoRows
		}
		return 0, fmt.Errorf("find active generation: %w", err)
	}
	// generation is NULL for a book whose graph was never started; the worker creates the
	// first generation when it prepares chapter records, and no run can match NULL.
	if _, err = tx.Exec(ctx, `DELETE FROM record_run
		WHERE novel_id=$1 AND generation_id=$2 AND status<>'published'
		  AND NOT (chapter_index = ANY($3))`, novelID, generation, busy); err != nil {
		return 0, fmt.Errorf("clear unfinished runs: %w", err)
	}
	rows, err := tx.Query(ctx, `UPDATE chapter AS c SET enrichment_discarded=false,
		enrichment_retry_at=NULL,enrichment_retry_generation_id=NULL,enrichment_attempts=0,
		provider_retry_at=NULL,provider_retry_generation_id=NULL,provider_retry_attempts=0,
		provider_retry_category=NULL
		WHERE c.novel_id=$1 AND (c.translation_ready OR c.status='done')
		  AND NOT (c.chapter_index = ANY($3))
		  AND NOT EXISTS (SELECT 1 FROM record_run r
		        WHERE r.novel_id=c.novel_id AND r.generation_id=$2
		          AND r.chapter_index=c.chapter_index AND r.status='published')
		RETURNING c.chapter_index`, novelID, generation, busy)
	if err != nil {
		return 0, fmt.Errorf("resume unfinished chapters: %w", err)
	}
	chapters := []int{}
	for rows.Next() {
		var index int
		if err := rows.Scan(&index); err != nil {
			rows.Close()
			return 0, err
		}
		chapters = append(chapters, index)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit records extract: %w", err)
	}
	sort.Ints(chapters)
	pin := ""
	if generation != nil {
		pin = *generation
	}
	// Chronological, like a rebuild: who's-who resolves against earlier published chapters.
	for _, index := range chapters {
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index, Enrichment: true, RecordGenerationID: pin}); err != nil {
			return 0, err
		}
	}
	return len(chapters), nil
}

// queuedChapters reports which of novelID's chapters have a pointer pending or claimed.
// enrichmentOnly narrows it to graph work; otherwise a translation pointer counts too,
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

// removeQueuedEnrichment drops the novel's queued graph work only. Translation shares
// this queue, and stopping the graph build must never cancel a chapter's prose: an
// unreadable chapter is a strictly worse outcome than an un-extracted one (§0).
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

// retryRendering cannot delete child rows from an already-published run: the publication
// is frozen as a whole, not just its record_run metadata. A rendering retry therefore
// opens a fresh generation and re-enriches chapters in order, just like a rebuild.
func (s *Store) retryRendering(ctx context.Context, novelID string, chapter int) error {
	_, _, err := s.rebuildRecords(ctx, novelID)
	return err
}

// rebuildRecords opens a fresh generation and re-enriches every saved chapter in order.
// The new generation becomes active immediately and starts empty: readers see pending
// knowledge while it fills, rather than a mix of two generations' identity decisions.
func (s *Store) rebuildRecords(ctx context.Context, novelID string) (string, int, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return "", 0, fmt.Errorf("begin records rebuild: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return "", 0, fmt.Errorf("lock records rebuild: %w", err)
	}
	var ontology []byte
	var predecessor, activeState string
	var hasPredecessor bool
	if err = tx.QueryRow(ctx, `SELECT ontology,COALESCE(active_record_generation::text,'')
		FROM novel WHERE id=$1 FOR UPDATE`, novelID).Scan(&ontology, &predecessor); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", 0, pgx.ErrNoRows
		}
		return "", 0, fmt.Errorf("find novel: %w", err)
	}
	if predecessor == "" {
		return "", 0, fmt.Errorf("%w: novel has no predecessor generation", ErrRecordsRebuildConflict)
	}
	if err = tx.QueryRow(ctx, `SELECT g.state,g.predecessor_generation_id IS NOT NULL
		FROM record_generation g WHERE g.id=$1 AND g.novel_id=$2`, predecessor, novelID).
		Scan(&activeState, &hasPredecessor); err != nil {
		return "", 0, fmt.Errorf("check rebuild state: %w", err)
	}
	eligible, _, missing, err := recordsRebuildCounts(ctx, tx, novelID, predecessor)
	if err != nil {
		return "", 0, fmt.Errorf("check rebuild completion: %w", err)
	}
	// A generation with a predecessor is a replacement. It remains in progress until it
	// has at least one eligible chapter and every eligible chapter has a published run;
	// an empty replacement has not actually completed any work and is still fenced.
	// Once complete, it may itself be the predecessor of the next rebuild (the old
	// `predecessor_generation_id IS NOT NULL` test incorrectly wedged every novel after
	// its first rebuild).
	if hasPredecessor && activeState == "active" && (eligible == 0 || missing > 0) {
		return "", 0, ErrRecordsRebuildActive
	}
	var predecessorArg any = predecessor
	var generation string
	if err = tx.QueryRow(ctx, `INSERT INTO record_generation
		(novel_id,state,ontology,prompt_version,checks_version,extraction_model,source_lang,target_lang,predecessor_generation_id)
		SELECT id,'active',ontology,'records-v1','records-v1','',source_lang,target_lang,$2
		  FROM novel WHERE id=$1
		RETURNING id::text`, novelID, predecessorArg).Scan(&generation); err != nil {
		return "", 0, fmt.Errorf("create generation: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='retired'
		WHERE novel_id=$1 AND id<>$2 AND state='active'`, novelID, generation); err != nil {
		return "", 0, fmt.Errorf("retire generations: %w", err)
	}
	if _, err = tx.Exec(ctx, "UPDATE novel SET active_record_generation=$2 WHERE id=$1",
		novelID, generation); err != nil {
		return "", 0, fmt.Errorf("activate generation: %w", err)
	}
	// A rebuild is an explicit "build everything" intent, so it lifts any pause a stop or
	// a per-chapter discard left behind. Without this the chapter below is re-enqueued and
	// then discarded again by the worker at its first records stage -- silently, on every
	// rebuild, with no surface anywhere saying why that chapter never fills in.
	if _, err = tx.Exec(ctx,
		"UPDATE chapter SET enrichment_discarded=false WHERE novel_id=$1 AND enrichment_discarded",
		novelID); err != nil {
		return "", 0, fmt.Errorf("resume discarded chapters: %w", err)
	}
	rows, err := tx.Query(ctx, "SELECT chapter_index FROM chapter WHERE novel_id=$1 ORDER BY chapter_index", novelID)
	if err != nil {
		return "", 0, fmt.Errorf("list chapters: %w", err)
	}
	chapters := []int{}
	for rows.Next() {
		var index int
		if err := rows.Scan(&index); err != nil {
			rows.Close()
			return "", 0, err
		}
		chapters = append(chapters, index)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return "", 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return "", 0, fmt.Errorf("commit records rebuild: %w", err)
	}
	// Chronological order matters: who's-who resolves a chapter against the identities
	// published before it, so enriching out of order would resolve against nothing.
	for _, index := range chapters {
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index, Enrichment: true, RecordGenerationID: generation}); err != nil {
			return generation, 0, err
		}
	}
	return generation, len(chapters), nil
}

// discardRecordsRebuild restores exactly the generation that was active when the
// replacement began. Published replacement runs are immutable history: they remain in
// the retired generation while the predecessor becomes active again. The novel advisory
// lock fences this transaction against both a publisher and another rebuild/discard request.
func (s *Store) discardRecordsRebuild(ctx context.Context, novelID, expectedGeneration string) error {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin records discard: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return fmt.Errorf("lock records discard: %w", err)
	}
	var active, predecessor, activeState, predecessorState string
	err = tx.QueryRow(ctx, `
		SELECT g.id::text,g.predecessor_generation_id::text,g.state,COALESCE(p.state,'')
		  FROM novel n
		  JOIN record_generation g ON g.id=n.active_record_generation
		  LEFT JOIN record_generation p ON p.id=g.predecessor_generation_id
		 WHERE n.id=$1
		 FOR UPDATE OF n,g`, novelID).Scan(&active, &predecessor, &activeState, &predecessorState)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgx.ErrNoRows
	}
	if err != nil {
		return fmt.Errorf("find active rebuild: %w", err)
	}
	if active != expectedGeneration || predecessor == "" || activeState != "active" || predecessorState != "retired" {
		return ErrRecordsRebuildConflict
	}
	eligible, _, missing, err := recordsRebuildCounts(ctx, tx, novelID, active)
	if err != nil {
		return fmt.Errorf("check rebuild completion: %w", err)
	}
	if eligible > 0 && missing == 0 {
		return fmt.Errorf("%w: replacement is complete", ErrRecordsRebuildConflict)
	}
	if _, err = tx.Exec(ctx, `UPDATE novel SET active_record_generation=$2 WHERE id=$1`, novelID, predecessor); err != nil {
		return fmt.Errorf("restore predecessor: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='active',retired_at=NULL WHERE id=$1`, predecessor); err != nil {
		return fmt.Errorf("reactivate predecessor: %w", err)
	}
	if _, err = tx.Exec(ctx, `UPDATE record_generation SET state='retired',retired_at=now()
		WHERE id=$1 AND novel_id=$2`, active, novelID); err != nil {
		return fmt.Errorf("retire unfinished replacement: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit records discard: %w", err)
	}
	return nil
}

func (s *Store) recordsRebuildStatus(ctx context.Context, novelID string) (RecordsRebuildStatus, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("begin records rebuild status: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if err = lockRecordsNovel(ctx, tx, novelID); err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("lock records rebuild status: %w", err)
	}
	var out RecordsRebuildStatus
	var active, predecessor *string
	if err = tx.QueryRow(ctx, `SELECT n.active_record_generation::text,COALESCE(g.state,''),
		g.predecessor_generation_id::text
		FROM novel n LEFT JOIN record_generation g ON g.id=n.active_record_generation
		WHERE n.id=$1`, novelID).Scan(&active, &out.ActiveState, &predecessor); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return RecordsRebuildStatus{}, pgx.ErrNoRows
		}
		return RecordsRebuildStatus{}, fmt.Errorf("find records generation: %w", err)
	}
	out.NovelID = novelID
	out.ActiveGenerationID = active
	out.PredecessorGenerationID = predecessor
	out.HasPredecessor = predecessor != nil
	if active != nil {
		out.EligibleChapters, out.PublishedChapters, out.MissingChapters, err = recordsRebuildCounts(ctx, tx, novelID, *active)
		if err != nil {
			return RecordsRebuildStatus{}, fmt.Errorf("count records rebuild: %w", err)
		}
	}
	if err = tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM chapter
		WHERE novel_id=$1 AND NOT enrichment_discarded
		  AND (enrichment_retry_at IS NOT NULL OR provider_retry_at IS NOT NULL))`,
		novelID).Scan(&out.Running); err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("check scheduled graph retries: %w", err)
	}
	if !out.Running {
		queued, qerr := s.queuedChapters(ctx, novelID, true)
		if qerr != nil {
			return RecordsRebuildStatus{}, qerr
		}
		out.Running = len(queued) > 0
	}
	out.Discardable = active != nil && predecessor != nil && out.ActiveState == "active" &&
		(out.EligibleChapters == 0 || out.MissingChapters > 0)
	if err = tx.Commit(ctx); err != nil {
		return RecordsRebuildStatus{}, fmt.Errorf("commit records rebuild status: %w", err)
	}
	return out, nil
}

func (a *API) recordsRetry(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.retryRecords(r.Context(), r.PathValue("id"), chapter)
	a.writeRecordsAction(w, "retry", map[string]any{"retried": true, "chapter_index": chapter}, err)
}

func (a *API) recordsDiscardChapter(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.discardChapterEnrichment(r.Context(), r.PathValue("id"), chapter)
	a.writeRecordsAction(w, "discard", map[string]any{"discarded": true, "chapter_index": chapter}, err)
}

func (a *API) recordsStop(w http.ResponseWriter, r *http.Request) {
	stopped, err := a.store.stopRecordsBuild(r.Context(), r.PathValue("id"))
	a.writeRecordsAction(w, "stop",
		map[string]any{"stopped": true, "chapters_stopped": stopped}, err)
}

func (a *API) recordsRenderRetry(w http.ResponseWriter, r *http.Request) {
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeErr(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	err = a.store.retryRendering(r.Context(), r.PathValue("id"), chapter)
	a.writeRecordsAction(w, "render-retry", map[string]any{"retried": true, "chapter_index": chapter}, err)
}

func (a *API) recordsExtract(w http.ResponseWriter, r *http.Request) {
	chapters, err := a.store.extractRecords(r.Context(), r.PathValue("id"))
	a.writeRecordsAction(w, "extract", map[string]any{"chapters_enqueued": chapters}, err)
}

func (a *API) recordsRebuild(w http.ResponseWriter, r *http.Request) {
	generation, chapters, err := a.store.rebuildRecords(r.Context(), r.PathValue("id"))
	a.writeRecordsAction(w, "rebuild",
		map[string]any{"generation_id": generation, "chapters_enqueued": chapters}, err)
}

func (a *API) recordsRebuildStatus(w http.ResponseWriter, r *http.Request) {
	status, err := a.store.recordsRebuildStatus(r.Context(), r.PathValue("id"))
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case err != nil:
		log.Printf("records rebuild status: %v", err)
		writeErr(w, http.StatusInternalServerError, "records status failed")
	default:
		writeJSON(w, http.StatusOK, status)
	}
}

func (a *API) recordsRebuildDiscard(w http.ResponseWriter, r *http.Request) {
	var request struct {
		GenerationID string `json:"generation_id"`
	}
	if err := decodeJSONBody(r, &request); err != nil || strings.TrimSpace(request.GenerationID) == "" {
		writeErr(w, http.StatusBadRequest, "generation_id is required")
		return
	}
	if _, err := uuid.Parse(request.GenerationID); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid generation_id")
		return
	}
	err := a.store.discardRecordsRebuild(r.Context(), r.PathValue("id"), request.GenerationID)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case errors.Is(err, ErrRecordsRebuildConflict), errors.Is(err, ErrRecordsRebuildActive):
		writeErr(w, http.StatusConflict, err.Error())
	case err != nil:
		log.Printf("records rebuild discard: %v", err)
		writeErr(w, http.StatusInternalServerError, "records action failed")
	default:
		writeJSON(w, http.StatusOK, map[string]any{"discarded": true, "generation_id": request.GenerationID})
	}
}

func (a *API) writeRecordsAction(w http.ResponseWriter, action string, body map[string]any, err error) {
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		writeErr(w, http.StatusNotFound, "no such novel")
	case errors.Is(err, ErrRecordsOutOfOrder):
		writeErr(w, http.StatusConflict, err.Error())
	case err != nil:
		log.Printf("records %s: %v", action, err)
		writeErr(w, http.StatusInternalServerError, "records action failed")
	default:
		writeJSON(w, http.StatusOK, body)
	}
}
