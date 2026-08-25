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
}

func (w *Worker) loadJob(ctx context.Context, jobID int64) (jobRow, error) {
	var row jobRow
	err := w.db.QueryRow(ctx,
		`SELECT novel_id::text, start_url, mode FROM scrape_job WHERE id = $1`, jobID,
	).Scan(&row.novelID, &row.startURL, &row.mode)
	return row, err
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

	onChapter := func(page Page) error {
		req := pasteChapterRequest{ChapterIndex: nextIndex, RawText: page.Text, SiteChapterNo: page.Title}
		if job.mode == "bootstrap" {
			req.TranslatedText = page.Text
		}
		if err := w.ingest.PasteChapter(ctx, job.novelID, req); err != nil {
			return err
		}
		nextIndex++
		_, err := w.db.Exec(ctx,
			`UPDATE scrape_job SET chapters_fetched = chapters_fetched + 1, updated_at = now() WHERE id = $1`,
			jobID,
		)
		return err
	}

	shouldStop := func(ctx context.Context) (bool, error) {
		return w.cancelRequested(ctx, jobID)
	}

	stopReason, walkErr := walk(ctx, w.client, site, job.startURL, onChapter, shouldStop, w.contentLenFloor)
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
