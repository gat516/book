package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/url"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

const (
	pendingQueue    = "scrape:pending" // keep in sync with reader-api's enqueue key
	processingQueue = "scrape:processing"
	// The PIPELINE's queue (services/pipeline/pipeline/worker.py's PENDING_QUEUE), read
	// here only to measure how far ahead of processing this scrape has run. The scraper
	// never writes it — ingest-api owns that.
	pipelinePendingQueue = "jobs:pending"
)

// Worker drains scrape:pending the same way pipeline/worker.py drains jobs:pending —
// BLMOVE to a processing list (not a plain pop) so a crash leaves the pointer
// recoverable, matching the fix applied there this session: go-redis's BLMove was
// verified to return redis.Nil (not an error) on a genuine empty-queue timeout, so no
// equivalent defensive catch is needed here (see fetch.go's doc comment for why the
// verification mattered).
type Worker struct {
	db              *pgxpool.Pool
	redis           *redis.Client
	ingest          *ingestClient
	client          *httpClient
	contentLenFloor int
	maxQueueDepth   int // service default; a scrape job may override it per novel
}

func NewWorker(cfg Config) (*Worker, error) {
	db, err := pgxpool.New(context.Background(), cfg.DatabaseURL)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	opts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, fmt.Errorf("parse redis url: %w", err)
	}
	return &Worker{
		db:              db,
		redis:           redis.NewClient(opts),
		ingest:          newIngestClient(cfg.IngestAPIURL),
		client:          newHTTPClient(cfg.ReqsPerSecond, cfg.JitterMillis, cfg.UserAgentString),
		contentLenFloor: cfg.ContentLenFloor,
		maxQueueDepth:   cfg.MaxQueueDepth,
	}, nil
}

func (w *Worker) Loop(ctx context.Context) error {
	for {
		raw, err := w.redis.BLMove(ctx, pendingQueue, processingQueue, "RIGHT", "LEFT", 5*time.Second).Result()
		if errors.Is(err, redis.Nil) {
			continue // empty queue timeout; poll again
		}
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			log.Printf("blmove error (retrying): %v", err)
			time.Sleep(time.Second)
			continue
		}

		jobID, parseErr := strconv.ParseInt(raw, 10, 64)
		if parseErr != nil {
			log.Printf("malformed queue message %q: %v", raw, parseErr)
			w.redis.LRem(ctx, processingQueue, 1, raw)
			continue
		}
		if err := w.handle(ctx, jobID); err != nil {
			log.Printf("scrape job %d failed: %v", jobID, err)
		}
		w.redis.LRem(ctx, processingQueue, 1, raw)
	}
}

type jobRow struct {
	novelID  string
	startURL string
	mode     string
	status   string
	// maxQueueDepth is per-scrape (nil = fall back to the service default): how far ahead
	// of the pipeline this novel may be fetched. Per novel rather than process-wide
	// because the right answer depends on the book — a short novel can be pulled in one
	// go, a 5000-chapter serial should not be.
	maxQueueDepth *int
}

func (w *Worker) loadJob(ctx context.Context, jobID int64) (jobRow, error) {
	var row jobRow
	err := w.db.QueryRow(ctx,
		`SELECT novel_id::text, start_url, mode, status, max_queue_depth FROM scrape_job WHERE id = $1`, jobID,
	).Scan(&row.novelID, &row.startURL, &row.mode, &row.status, &row.maxQueueDepth)
	return row, err
}

// RecoverProcessing returns queue pointers stranded by a scraper restart to pending.
// A scrape can safely resume from its start URL because content-hash dedup skips pages
// already stored; terminal jobs are discarded rather than accidentally replayed.
func (w *Worker) RecoverProcessing(ctx context.Context) error {
	raws, err := w.redis.LRange(ctx, processingQueue, 0, -1).Result()
	if err != nil {
		return err
	}
	for _, raw := range raws {
		jobID, parseErr := strconv.ParseInt(raw, 10, 64)
		if parseErr != nil {
			if err := w.redis.LRem(ctx, processingQueue, 1, raw).Err(); err != nil {
				return err
			}
			continue
		}
		job, loadErr := w.loadJob(ctx, jobID)
		if loadErr != nil || (job.status != "pending" && job.status != "running") {
			if err := w.redis.LRem(ctx, processingQueue, 1, raw).Err(); err != nil {
				return err
			}
			continue
		}
		if err := w.redis.LRem(ctx, processingQueue, 1, raw).Err(); err != nil {
			return err
		}
		if err := w.redis.LPush(ctx, pendingQueue, raw).Err(); err != nil {
			return err
		}
		log.Printf("requeued interrupted scrape job %d", jobID)
	}
	return nil
}

// waitForCapacity blocks while the pipeline's pending queue is at or above this scrape's
// depth limit, so fetching cannot outrun processing.
//
// Fetching is network-bound and processing is LLM-bound — measured on this stack, 145
// chapters were fetched in 30 minutes while none finished translating. Left uncapped, a
// long novel is pulled down in hours onto a queue that takes weeks to drain, and the
// source site is hammered for content nothing can use yet.
//
// A depth of 0 disables the limit (fetch as fast as politeness allows).
//
// ingestLookahead bounds how far AHEAD OF THE READER this novel may be fetched, which is
// the limit that actually bites: since the scraper stopped queueing translation for what
// it ingests, the pipeline queue sits near empty and the depth check below almost never
// fires. The depth check is kept as a safety net for whenever something else is filling
// that queue. 0 disables either limit independently.
func (w *Worker) waitForCapacity(
	jobID int64, novelID string, limit, ingestLookahead int,
) func(context.Context) error {
	return func(ctx context.Context) error {
		if limit <= 0 && ingestLookahead <= 0 {
			return nil
		}
		logged := false
		for {
			ahead, err := w.chaptersAheadOfReader(ctx, novelID)
			if err != nil {
				log.Printf("scrape job %d: reader-window check failed, continuing: %v", jobID, err)
			} else if ingestLookahead > 0 && ahead >= ingestLookahead {
				if !logged {
					log.Printf("scrape job %d: pausing, %d chapters ahead of the reader (limit %d)",
						jobID, ahead, ingestLookahead)
					logged = true
				}
				if err := w.pauseUntilNextCheck(ctx, jobID); err != nil {
					return err
				}
				continue
			}

			if limit <= 0 {
				return nil
			}
			depth, err := w.redis.LLen(ctx, pipelinePendingQueue).Result()
			if err != nil {
				// Don't strand a scrape on a transient Redis blip: the limit is a
				// courtesy throttle, not a correctness invariant.
				log.Printf("scrape job %d: queue depth check failed, continuing: %v", jobID, err)
				return nil
			}
			if depth < int64(limit) {
				return nil
			}
			if !logged {
				log.Printf("scrape job %d: pausing, pipeline queue at %d (limit %d)", jobID, depth, limit)
				logged = true
			}
			// Cancellation must still be honoured while paused, otherwise a cancel
			// request would not take effect until the queue drained.
			cancelled, err := w.cancelRequested(ctx, jobID)
			if err == nil && cancelled {
				return nil // let the walk loop's own shouldStop observe it and stop cleanly
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(5 * time.Second):
			}
		}
	}
}

// chaptersAheadOfReader is how many chapters this novel holds beyond the furthest point
// any reader has reached. Uses MAX(current_chapter) across readers so one reader lagging
// doesn't stall fetching for another who is further along; a novel nobody has opened
// reports its full chapter count, which is what makes the very first scrape stop at the
// lookahead rather than pulling the whole serial down.
func (w *Worker) chaptersAheadOfReader(ctx context.Context, novelID string) (int, error) {
	var ahead int
	err := w.db.QueryRow(ctx,
		`SELECT COALESCE((SELECT MAX(chapter_index) FROM chapter WHERE novel_id = $1), 0)
		      - COALESCE((SELECT MAX(current_chapter) FROM reader_progress WHERE novel_id = $1), 0)`,
		novelID,
	).Scan(&ahead)
	return ahead, err
}

// pauseUntilNextCheck waits before re-evaluating a limit, while still honouring
// cancellation — otherwise a cancel wouldn't take effect until the window reopened.
func (w *Worker) pauseUntilNextCheck(ctx context.Context, jobID int64) error {
	cancelled, err := w.cancelRequested(ctx, jobID)
	if err == nil && cancelled {
		return nil // the walk loop's own shouldStop observes it and stops cleanly
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(5 * time.Second):
		return nil
	}
}

// novelIngestLookahead reads how far ahead of the reader this novel may be fetched
// (migration 0018).
func (w *Worker) novelIngestLookahead(ctx context.Context, novelID string) (int, error) {
	var lookahead int
	err := w.db.QueryRow(ctx,
		`SELECT ingest_lookahead FROM novel WHERE id = $1`, novelID,
	).Scan(&lookahead)
	return lookahead, err
}

func (w *Worker) setStatus(ctx context.Context, jobID int64, status, lastError string) error {
	_, err := w.db.Exec(ctx,
		`UPDATE scrape_job SET status = $1, last_error = NULLIF($2, ''), updated_at = now() WHERE id = $3`,
		status, lastError, jobID,
	)
	return err
}

func (w *Worker) cancelRequested(ctx context.Context, jobID int64) (bool, error) {
	var cancelled bool
	err := w.db.QueryRow(ctx, `SELECT cancel_requested FROM scrape_job WHERE id = $1`, jobID).Scan(&cancelled)
	return cancelled, err
}

func (w *Worker) nextChapterIndex(ctx context.Context, novelID string) (int, error) {
	var maxIndex int
	err := w.db.QueryRow(ctx,
		`SELECT COALESCE(MAX(chapter_index), 0) FROM chapter WHERE novel_id = $1`, novelID,
	).Scan(&maxIndex)
	return maxIndex + 1, err
}

func (w *Worker) handle(ctx context.Context, jobID int64) error {
	job, err := w.loadJob(ctx, jobID)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("job %d not found", jobID)
	}
	if err != nil {
		return err
	}
	if job.status != "pending" && job.status != "running" {
		return nil
	}

	parsed, err := url.Parse(job.startURL)
	if err != nil {
		return w.fail(ctx, jobID, fmt.Sprintf("invalid start_url: %v", err))
	}
	site := siteFor(parsed.Host)
	if site == nil {
		return w.fail(ctx, jobID, fmt.Sprintf("unsupported site host %q", parsed.Host))
	}

	if err := w.setStatus(ctx, jobID, "running", ""); err != nil {
		return err
	}

	nextIndex, err := w.nextChapterIndex(ctx, job.novelID)
	if err != nil {
		return w.fail(ctx, jobID, err.Error())
	}

	// Part tracking (see SourceMeta.Part in ingest-api): this site serves one source
	// chapter as several paginated pages, each of which becomes its own chapter row. Pages
	// of the same chapter carry an identical title, so a title change is the chapter
	// boundary and the part counter restarts there.
	//
	// Note the counter starts from part 1 at the START URL, so a scrape that begins
	// mid-chapter labels that first partial chapter from 1 rather than its true part
	// number — the site does not expose one, and only the first chapter of a run is
	// affected.
	previousTitle := ""
	part := 0

	onChapter := func(page Page) error {
		if page.Title == previousTitle {
			part++
		} else {
			part = 1
			previousTitle = page.Title
		}
		req := pasteChapterRequest{
			ChapterIndex:  nextIndex,
			RawText:       page.Text,
			SiteChapterNo: page.Title,
			SourceURL:     page.SourceURL,
			Part:          part,
			Enqueue:       false, // reader-api queues translation on demand; see the field's comment
		}
		if job.mode == "bootstrap" {
			req.TranslatedText = page.Text
		}
		duplicate, err := w.ingest.PasteChapter(ctx, job.novelID, req)
		if err != nil {
			return err
		}
		if duplicate {
			// This novel already holds this exact body (migration 0013). Nothing was
			// written, so nextIndex must NOT advance — otherwise the next genuinely-new
			// page would be inserted at a gapped index. Keep walking rather than stopping:
			// re-running a scrape from the original start URL crosses the whole
			// already-ingested prefix before reaching new chapters, and stopping at the
			// first duplicate would mean it never gets there.
			log.Printf("scrape job %d: skipping already-ingested page %q", jobID, page.Title)
			return nil
		}
		nextIndex++

		// Keep translation following ingestion. Bounded by the novel's translate_lookahead
		// and idempotent, so this tops the window up rather than queueing everything
		// fetched — without it a freshly scraped novel sits at zero translated until a
		// reader happens to open a chapter.
		//
		// Best-effort: failing to queue means translation lags, not that the chapter
		// failed to ingest, so it must never fail the scrape.
		if err := w.ingest.TranslateAhead(ctx, job.novelID); err != nil {
			log.Printf("scrape job %d: could not top up translate window: %v", jobID, err)
		}

		_, err = w.db.Exec(ctx,
			`UPDATE scrape_job SET chapters_fetched = chapters_fetched + 1, updated_at = now() WHERE id = $1`,
			jobID,
		)
		return err
	}

	shouldStop := func(ctx context.Context) (bool, error) {
		return w.cancelRequested(ctx, jobID)
	}

	// Per-scrape limit wins; the service default applies when the job didn't specify one.
	queueLimit := w.maxQueueDepth
	if job.maxQueueDepth != nil {
		queueLimit = *job.maxQueueDepth
	}
	ingestLookahead, err := w.novelIngestLookahead(ctx, job.novelID)
	if err != nil {
		return w.fail(ctx, jobID, err.Error())
	}

	stopReason, walkErr := walk(ctx, w.client, site, job.startURL, onChapter, shouldStop,
		w.waitForCapacity(jobID, job.novelID, queueLimit, ingestLookahead), w.contentLenFloor)
	if walkErr != nil {
		return w.fail(ctx, jobID, walkErr.Error())
	}

	finalStatus := "done"
	if stopReason == "cancelled" {
		finalStatus = "cancelled"
	}
	log.Printf("scrape job %d: %s (%s)", jobID, finalStatus, stopReason)
	return w.setStatus(ctx, jobID, finalStatus, "")
}

func (w *Worker) fail(ctx context.Context, jobID int64, message string) error {
	if err := w.setStatus(ctx, jobID, "error", message); err != nil {
		return err
	}
	return fmt.Errorf("%s", message)
}
