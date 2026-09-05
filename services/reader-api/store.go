package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/minio/minio-go/v7"
	"github.com/redis/go-redis/v9"
)

var (
	ErrNotFound        = errors.New("not found")
	ErrChapterNotReady = errors.New("chapter is not ready")
	ErrScrapeJobActive = errors.New("a scrape is already running for this novel")
)

const (
	readerRole   = "rls_reader"
	progressRole = "reader_progress_writer"
	// operatorRole may execute repair_preview and nothing else. rls_reader is deliberately
	// NOT a member: askai connects as rls_reader, and the preview carries source quotes
	// from every snapshotted chapter regardless of reading progress (migration 0046).
	operatorRole = "repair_operator"
)

type ReaderStore interface {
	Health(context.Context) error
	GetProgress(context.Context, string, string) (Progress, error)
	AdvanceProgress(context.Context, string, string, int) (Progress, error)
	GetEntity(context.Context, string, string, int) (EntityView, error)
	ListWiki(context.Context, string, int) ([]EntitySummary, error)
	ListTimeline(context.Context, string, int) ([]EventView, error)
	EventStatus(context.Context, string, int, int) (KnowledgeStatus, error)
	ListRelationships(context.Context, string, string, int) ([]RelationshipView, error)
	GetChapter(context.Context, string, int, int) (ChapterView, error)
	KnowledgeStatus(context.Context, string, int, int) (KnowledgeStatus, error)
	ListChapters(context.Context, string, int, int) ([]ChapterListItem, int, error)
	PipelineStatus(context.Context, string) (PipelineStatusResponse, error)
	TranslationPreview(context.Context, string, int) (string, bool, string, error)
	TranslationHealth(context.Context, string) (TranslationHealth, error)
	RepairStatus(context.Context, string) (RepairStatus, error)
	RepairPreview(context.Context, string, string) (RepairPreview, error)
	RepairProgress(context.Context, string) ([]RepairProgressFact, error)
	ListNovels(context.Context) ([]NovelSummary, error)
	GetNovel(context.Context, string) (NovelSummary, error)
	CreateScrapeJob(context.Context, string, string, string) (int64, error)
	LatestScrapeJob(context.Context, string) (ScrapeJobView, error)
	RequestScrapeCancel(context.Context, string) error
	ListGlossary(context.Context, string, int) ([]GlossaryTermView, error)
	ListNameReviews(context.Context, string, *int) ([]CharacterNameReview, error)
}

func (s *Store) ListNameReviews(ctx context.Context, novelID string, chapter *int) ([]CharacterNameReview, error) {
	rows, err := s.readerDB.Query(ctx, `SELECT r.source_term,r.first_seen_chapter,r.quote,r.reason,r.candidates,r.term_role,r.rendering_method
		FROM character_name_review r WHERE r.novel_id=$1 AND r.status='pending'
		AND ($2::int IS NULL OR EXISTS (SELECT 1 FROM character_name_occurrence o
			WHERE o.novel_id=r.novel_id AND o.source_term=r.source_term AND o.chapter_index=$2))
		ORDER BY r.first_seen_chapter,r.source_term`, novelID, chapter)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	reviews := []CharacterNameReview{}
	for rows.Next() {
		var review CharacterNameReview
		var candidates []byte
		if err := rows.Scan(&review.SourceTerm, &review.FirstSeenChapter, &review.Quote, &review.Reason, &candidates, &review.TermRole, &review.RenderingMethod); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(candidates, &review.Candidates); err != nil {
			return nil, err
		}
		reviews = append(reviews, review)
	}
	return reviews, rows.Err()
}

type Store struct {
	readerDB   *pgxpool.Pool
	progressDB *pgxpool.Pool
	// operatorDB is used by exactly one method, RepairPreview. Do not reach for it because
	// a query is inconvenient on readerDB — its whole value is that it is narrow.
	operatorDB *pgxpool.Pool
	objects    *minio.Client
	bucket     string
	redis      *redis.Client
}

// scrapePendingQueue must match services/scraper/job.go's pendingQueue constant.
const scrapePendingQueue = "scrape:pending"

// These three must match services/pipeline/pipeline/worker.py's constants of the same
// names — they are the pipeline's own queue keys, read here (never written) purely to
// report what the worker is doing.
const (
	pipelinePendingQueue    = "jobs:pending"
	pipelineProcessingQueue = "jobs:processing"
	pipelineProcessingStart = "jobs:processing:started"
	pipelineProcessingStage = "jobs:processing:stage"
	pipelineStageStart      = "jobs:processing:stage:started"
	pipelineWorkerHeartbeat = "jobs:worker:heartbeat"
	// Partial translation text for a chapter still in flight. Format must match
	// services/pipeline/pipeline/worker.py's PREVIEW_KEY.
	pipelinePreviewKeyFmt = "translate:preview:%s:%d"
)

func newRolePool(ctx context.Context, databaseURL, role string) (*pgxpool.Pool, error) {
	config, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse %s database URL: %w", role, err)
	}
	config.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, "SET ROLE "+pgx.Identifier{role}.Sanitize())
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("create %s pool: %w", role, err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping %s pool: %w", role, err)
	}
	return pool, nil
}

func newStore(ctx context.Context, cfg Config, objects *minio.Client, redisClient *redis.Client) (*Store, error) {
	// Opened in a loop rather than with a cascade of hand-written Close() calls: with two
	// pools the cascade was fine, with three it is a shape that silently leaks the day
	// someone adds a fourth and forgets a branch.
	specs := []struct {
		url  string
		role string
	}{
		{cfg.ReaderDatabaseURL, readerRole},
		{cfg.ProgressDatabaseURL, progressRole},
		{cfg.RepairOperatorDatabaseURL, operatorRole},
	}
	pools := make([]*pgxpool.Pool, 0, len(specs))
	for _, spec := range specs {
		pool, err := newRolePool(ctx, spec.url, spec.role)
		if err != nil {
			for _, opened := range pools {
				opened.Close()
			}
			return nil, err
		}
		pools = append(pools, pool)
	}
	return &Store{
		readerDB:   pools[0],
		progressDB: pools[1],
		operatorDB: pools[2],
		objects:    objects,
		bucket:     cfg.ObjectBucket,
		redis:      redisClient,
	}, nil
}

func (s *Store) Close() {
	s.readerDB.Close()
	s.progressDB.Close()
	s.operatorDB.Close()
}

func (s *Store) Health(ctx context.Context) error {
	if err := s.readerDB.Ping(ctx); err != nil {
		return fmt.Errorf("reader database: %w", err)
	}
	if err := s.progressDB.Ping(ctx); err != nil {
		return fmt.Errorf("progress database: %w", err)
	}
	if err := s.operatorDB.Ping(ctx); err != nil {
		return fmt.Errorf("repair operator database: %w", err)
	}
	return nil
}

func (s *Store) GetProgress(ctx context.Context, readerID, novelID string) (Progress, error) {
	var progress Progress
	err := s.progressDB.QueryRow(ctx,
		`SELECT novel_id::text, reader_id, current_chapter, updated_at
		 FROM reader_progress WHERE reader_id = $1 AND novel_id = $2`,
		readerID, novelID,
	).Scan(&progress.NovelID, &progress.ReaderID, &progress.CurrentChapter, &progress.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return Progress{}, ErrNotFound
	}
	return progress, err
}

func (s *Store) AdvanceProgress(
	ctx context.Context, readerID, novelID string, chapter int,
) (Progress, error) {
	var progress Progress
	err := s.progressDB.QueryRow(ctx,
		`INSERT INTO reader_progress (reader_id, novel_id, current_chapter)
		 SELECT $1, c.novel_id, c.chapter_index
		 FROM chapter c
		 WHERE c.novel_id = $2 AND c.chapter_index = $3 AND (c.translation_ready OR c.status = 'done')
		 ON CONFLICT (reader_id, novel_id) DO UPDATE
		 SET current_chapter = GREATEST(reader_progress.current_chapter, EXCLUDED.current_chapter),
		     updated_at = now()
		 RETURNING novel_id::text, reader_id, current_chapter, updated_at`,
		readerID, novelID, chapter,
	).Scan(&progress.NovelID, &progress.ReaderID, &progress.CurrentChapter, &progress.UpdatedAt)
	if err == nil {
		return progress, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return Progress{}, err
	}

	var exists bool
	if lookupErr := s.progressDB.QueryRow(ctx,
		`SELECT EXISTS (SELECT 1 FROM novel WHERE id = $1)`, novelID,
	).Scan(&exists); lookupErr != nil {
		return Progress{}, lookupErr
	}
	if !exists {
		return Progress{}, ErrNotFound
	}
	return Progress{}, ErrChapterNotReady
}

// ListNovels and GetNovel are ungated: novel metadata (title/langs/genre/created_at) has
// no source_chapter column to gate on, so gating it would be theater, not security. Both
// query readerDB directly (no withReaderTx/SET LOCAL) since `novel` carries no RLS policy
// (0002_rls.sql enables it only on fact/edge/event/chunk/entity/alias).
func (s *Store) ListNovels(ctx context.Context) ([]NovelSummary, error) {
	rows, err := s.readerDB.Query(ctx,
		`SELECT id::text, title, source_lang, target_lang, genre, created_at
		 FROM novel ORDER BY created_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	novels := []NovelSummary{}
	for rows.Next() {
		var novel NovelSummary
		if err := rows.Scan(
			&novel.ID, &novel.Title, &novel.SourceLang, &novel.TargetLang,
			&novel.Genre, &novel.CreatedAt,
		); err != nil {
			return nil, err
		}
		novels = append(novels, novel)
	}
	return novels, rows.Err()
}

// ListChapters pages the chapter index for a novel. Deliberately NOT progress-gated: this
// returns navigation/ingestion metadata (index, the site's own printed label, pipeline
// status) and never chapter text — the spoiler gate that matters stays fully enforced in
// GetChapter, which still refuses any chapter above stored progress. Without an ungated
// index there is no way to navigate to, or even see the existence of, a chapter you have
// not reached, which is what makes a several-thousand-chapter novel usable at all.
//
// Uses progressDB (not readerDB/withReaderTx) for the same reason GetChapter does: `chapter`
// carries no RLS policy, and this is a plain metadata read with no gate columns to set.
func (s *Store) ListChapters(ctx context.Context, novelID string, limit, offset int) ([]ChapterListItem, int, error) {
	var total int
	if err := s.progressDB.QueryRow(ctx,
		`SELECT count(*) FROM chapter WHERE novel_id = $1`, novelID,
	).Scan(&total); err != nil {
		return nil, 0, err
	}

	// COALESCE(..., 1): rows ingested before Part existed carry no 'part' key, and an
	// ordinary non-paginated chapter is part 1 by definition — so the absent case and the
	// default case are the same answer.
	rows, err := s.progressDB.Query(ctx,
		`SELECT chapter_index, source_meta->>'site_chapter_no', source_meta->>'source_url',
		        COALESCE((source_meta->>'part')::int, 1),
		        CASE WHEN translation_ready THEN 'done' ELSE status END,
		        CASE WHEN status='done' THEN 'done' WHEN status='error' THEN 'error' ELSE 'pending' END,
		        translation_warning_code,translation_warning_count
		 FROM chapter WHERE novel_id = $1
		 ORDER BY chapter_index
		 LIMIT $2 OFFSET $3`,
		novelID, limit, offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	chapters := []ChapterListItem{}
	for rows.Next() {
		var item ChapterListItem
		var siteChapterNo, sourceURL *string
		var warningCode *string
		var warningCount int
		if err := rows.Scan(&item.ChapterIndex, &siteChapterNo, &sourceURL, &item.Part, &item.Status, &item.GraphStatus, &warningCode, &warningCount); err != nil {
			return nil, 0, err
		}
		if siteChapterNo != nil {
			item.SiteChapterNo = *siteChapterNo
		}
		if sourceURL != nil {
			item.SourceURL = *sourceURL
		}
		if warningCode != nil {
			item.TranslationWarning = &TranslationWarning{Code: *warningCode, TermCount: warningCount}
		}
		chapters = append(chapters, item)
	}
	return chapters, total, rows.Err()
}

// PipelineStatus reports the worker's live queue state for one novel. Read-only against
// the pipeline's Redis keys — reader-api never writes them; the worker owns that queue.
//
// Errors from the individual per-claim lookups are swallowed rather than failing the whole
// request: this is an observability endpoint, and a partially-populated answer ("chapter 1
// in flight, stage unknown") is far more useful to someone staring at a stuck reader than
// a 500.
func (s *Store) PipelineStatus(ctx context.Context, novelID string) (PipelineStatusResponse, error) {
	status := PipelineStatusResponse{NovelID: novelID, InFlight: []InFlightChapter{}}

	pending, err := s.redis.LRange(ctx, pipelinePendingQueue, 0, -1).Result()
	if err != nil {
		return PipelineStatusResponse{}, fmt.Errorf("pending queue: %w", err)
	}
	status.Pending = len(pending)
	for _, raw := range pending {
		var msg struct {
			NovelID string `json:"novel_id"`
		}
		if json.Unmarshal([]byte(raw), &msg) == nil && msg.NovelID == novelID {
			status.PendingForNovel++
		}
	}

	// The worker refreshes this expiring key while idle and during long model calls. Its
	// presence distinguishes an ordinary queue wait from work that cannot advance because
	// the worker process is stopped.
	if online, err := s.redis.Exists(ctx, pipelineWorkerHeartbeat).Result(); err == nil {
		status.WorkerOnline = online > 0
	}
	if mode, err := s.redis.HGet(ctx, "jobs:control", "mode").Result(); err == nil {
		status.QueueMode = mode
	}
	if status.QueueMode == "" {
		status.QueueMode = "all"
	}

	claims, err := s.redis.LRange(ctx, pipelineProcessingQueue, 0, -1).Result()
	if err != nil {
		return PipelineStatusResponse{}, fmt.Errorf("processing queue: %w", err)
	}

	for _, raw := range claims {
		var msg struct {
			NovelID      string `json:"novel_id"`
			ChapterIndex int    `json:"chapter_index"`
		}
		if err := json.Unmarshal([]byte(raw), &msg); err != nil || msg.NovelID != novelID {
			continue
		}
		item := InFlightChapter{ChapterIndex: msg.ChapterIndex}
		if stage, err := s.redis.HGet(ctx, pipelineProcessingStage, raw).Result(); err == nil {
			item.Stage = stage
		}
		if startedAt, err := s.redis.HGet(ctx, pipelineProcessingStart, raw).Float64(); err == nil {
			if elapsed := time.Since(time.Unix(int64(startedAt), 0)).Seconds(); elapsed > 0 {
				item.ElapsedSecs = int(elapsed)
			}
		}
		if startedAt, err := s.redis.HGet(ctx, pipelineStageStart, raw).Float64(); err == nil {
			if elapsed := time.Since(time.Unix(int64(startedAt), 0)).Seconds(); elapsed > 0 {
				item.StageElapsedSecs = int(elapsed)
			}
		}
		status.InFlight = append(status.InFlight, item)
	}
	return status, nil
}

// TranslationPreview returns the partial translation of a chapter currently being
// translated, and whether one exists at all. Absent is the normal case: the key only lives
// while the TRANSLATE stage is streaming, and the worker deletes it once the chapter is
// readable for real.
//
// Not progress-gated, and that is a deliberate narrow exception rather than an oversight.
// A chapter being translated is by definition not 'done', so stored progress can never
// have reached it — gating on progress would make the preview permanently unreachable and
// the feature pointless. What the gate actually protects is incidentally learning future
// facts (hover cards, Ask-AI drawing on unread chapters); this shows only the one chapter
// the reader deliberately opened and is waiting on. GetChapter's gate is untouched.
func (s *Store) TranslationPreview(ctx context.Context, novelID string, chapterIndex int) (string, bool, string, error) {
	// Chapter status comes back with the preview so one poll answers both "how far along"
	// and "can I read it now". A chapter with no row at all reports "" rather than
	// erroring — the caller renders that the same as "nothing to show yet".
	var status string
	err := s.progressDB.QueryRow(ctx,
		`SELECT CASE WHEN translation_ready THEN 'done' ELSE status END FROM chapter WHERE novel_id = $1 AND chapter_index = $2`,
		novelID, chapterIndex,
	).Scan(&status)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return "", false, "", fmt.Errorf("read chapter status: %w", err)
	}

	if status == "done" {
		return "", false, status, nil
	}
	text, err := s.redis.Get(ctx, fmt.Sprintf(pipelinePreviewKeyFmt, novelID, chapterIndex)).Result()
	if errors.Is(err, redis.Nil) {
		return "", false, status, nil
	}
	if err != nil {
		return "", false, status, fmt.Errorf("read translation preview: %w", err)
	}
	return text, true, status, nil
}

// Thresholds for warning about translation instability. Deliberately conservative: a
// false alarm trains the reader to ignore the notice, which costs more than staying quiet.
const (
	// Below this many observed terms there isn't enough evidence to judge — an early
	// chapter with two competing names is normal, not a problem.
	healthMinTermsObserved = 8
	// Fraction of observed terms the model is naming inconsistently.
	healthUnstableFraction = 0.25
	// Enough rejected chapters that the cause is systematic rather than incidental.
	healthFailedChapters = 3
)

// TranslationHealth measures how consistently this novel's terminology is being
// translated. See the TranslationHealth type for why glossary_candidate is the signal.
func (s *Store) TranslationHealth(ctx context.Context, novelID string) (TranslationHealth, error) {
	health := TranslationHealth{NovelID: novelID}

	// glossary and glossary_candidate are readable by the reader role (0010, 0017).
	// ProvisionalTerms counts DISTINCT source terms still awaiting corroboration, and is
	// counted separately from unstable ones for a reason found by testing: deriving the
	// sample size from locked+unstable alone means a model so inconsistent that nothing
	// ever gets locked reports a tiny sample and never trips the threshold — staying
	// silent in exactly the worst case. Provisional terms are the bulk of the evidence
	// early on, so they belong in the denominator.
	if err := s.readerDB.QueryRow(ctx,
		`SELECT
		   (SELECT count(*) FROM glossary WHERE novel_id = $1 AND NOT deleted),
		   (SELECT count(*) FROM (
		      SELECT source_term FROM glossary_candidate
		      WHERE novel_id = $1
		      GROUP BY source_term
		      HAVING count(DISTINCT target_term) > 1
		    ) competing),
		   (SELECT count(DISTINCT source_term) FROM glossary_candidate WHERE novel_id = $1)`,
		novelID,
	).Scan(&health.LockedTerms, &health.UnstableTerms, &health.ProvisionalTerms); err != nil {
		return TranslationHealth{}, fmt.Errorf("read terminology stability: %w", err)
	}

	// chapter lives on the progress pool, same as everywhere else that reads it.
	if err := s.progressDB.QueryRow(ctx,
		`SELECT count(*) FILTER (WHERE status = 'error' AND NOT translation_ready),
		        count(*) FILTER (WHERE translation_warning_code IS NOT NULL)
		 FROM chapter WHERE novel_id = $1`, novelID,
	).Scan(&health.FailedChapters, &health.WarningChapters); err != nil {
		return TranslationHealth{}, fmt.Errorf("count failed chapters: %w", err)
	}

	// Promotion deletes a term's candidates, so locked and provisional never double-count.
	observed := health.LockedTerms + health.ProvisionalTerms
	switch {
	case health.FailedChapters >= healthFailedChapters:
		health.Warn = true
		health.Reason = "several chapters were rejected because the translation didn't use the locked names consistently"
	case health.WarningChapters > 0:
		health.Warn = true
		health.Reason = "some readable chapters could not preserve every locked name"
	case observed >= healthMinTermsObserved &&
		float64(health.UnstableTerms) >= float64(observed)*healthUnstableFraction:
		health.Warn = true
		health.Reason = "the model is giving the same names different translations between chapters"
	}
	return health, nil
}

func (s *Store) GetNovel(ctx context.Context, novelID string) (NovelSummary, error) {
	var novel NovelSummary
	err := s.readerDB.QueryRow(ctx,
		`SELECT id::text, title, source_lang, target_lang, genre, created_at
		 FROM novel WHERE id = $1`, novelID,
	).Scan(&novel.ID, &novel.Title, &novel.SourceLang, &novel.TargetLang, &novel.Genre, &novel.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return NovelSummary{}, ErrNotFound
	}
	return novel, err
}

func (s *Store) withReaderTx(
	ctx context.Context, novelID string, at int, operation func(pgx.Tx) error,
) error {
	tx, err := s.readerDB.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()

	if _, err := tx.Exec(ctx,
		`SELECT set_config('app.novel_id', $1, true),
		        set_config('app.current_chapter', $2, true)`,
		novelID, fmt.Sprintf("%d", at),
	); err != nil {
		return err
	}
	if err := operation(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) GetEntity(
	ctx context.Context, novelID, entityID string, at int,
) (EntityView, error) {
	view := EntityView{Aliases: []string{}, Facts: []FactView{}, Renderings: []TermRenderingView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		var err error
		view.Knowledge, err = knowledgeInTx(ctx, tx, at)
		if err != nil {
			return err
		}
		if err := tx.QueryRow(ctx,
			`SELECT id::text, canonical, kind, first_seen_chapter
			 FROM entity
			 WHERE novel_id = $1 AND id = $2 AND first_seen_chapter <= $3`,
			novelID, entityID, at,
		).Scan(&view.ID, &view.Canonical, &view.Kind, &view.FirstSeenChapter); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			return err
		}

		aliasRows, err := tx.Query(ctx,
			`SELECT surface FROM alias
			 WHERE entity_id = $1 AND first_seen_chapter <= $2
			 ORDER BY first_seen_chapter, surface`, entityID, at)
		if err != nil {
			return err
		}
		defer aliasRows.Close()
		for aliasRows.Next() {
			var alias string
			if err := aliasRows.Scan(&alias); err != nil {
				return err
			}
			view.Aliases = append(view.Aliases, alias)
		}
		if err := aliasRows.Err(); err != nil {
			return err
		}

		// Locked glossary decisions are attached through the revision-specific binding,
		// never by matching a displayed name back to an entity. That preserves RESOLVE's
		// sole ownership of identity while making the existing alternatives available at
		// the exact place a reader encounters the term.
		renderingRows, err := tx.Query(ctx,
			`SELECT g.source_term,g.target_term,'locked',COALESCE(r.term_role,''),
			        COALESCE(r.candidates,'[]'::jsonb)
			 FROM glossary g
			 LEFT JOIN glossary_binding gb ON gb.novel_id=g.novel_id AND gb.source_term=g.source_term
			 LEFT JOIN character_name_review r ON r.novel_id=g.novel_id
			   AND r.source_term=g.source_term AND r.first_seen_chapter <= $3
			 WHERE g.novel_id=$1 AND COALESCE(gb.entity_id,g.entity_id)=$2
			   AND g.locked_at_chapter <= $3 AND NOT g.deleted
			 UNION ALL
			 SELECT DISTINCT r.source_term,NULL::text,'pending',r.term_role,r.candidates
			 FROM character_name_review r
			 JOIN source_mention m ON m.novel_id=r.novel_id AND m.surface=r.source_term
			 JOIN mention_binding b ON b.revision_id=m.revision_id AND b.mention_id=m.id
			 WHERE r.novel_id=$1 AND b.entity_id=$2 AND r.status='pending'
			   AND r.first_seen_chapter <= $3 AND b.known_from_chapter <= $3
			 ORDER BY 1`, novelID, entityID, at)
		if err != nil {
			return err
		}
		defer renderingRows.Close()
		for renderingRows.Next() {
			var rendering TermRenderingView
			var candidates []byte
			if err := renderingRows.Scan(
				&rendering.SourceTerm, &rendering.TargetTerm, &rendering.Status,
				&rendering.TermRole, &candidates,
			); err != nil {
				return err
			}
			if err := json.Unmarshal(candidates, &rendering.Candidates); err != nil {
				return err
			}
			view.Renderings = append(view.Renderings, rendering)
		}
		if err := renderingRows.Err(); err != nil {
			return err
		}

		factRows, err := tx.Query(ctx,
			`WITH visible AS (
			   SELECT id, attribute, value, kind, supersedes, valid_from_chapter,
			          source_chapter, confidence, evidence_id
			   FROM fact
			   WHERE novel_id = $1 AND entity_id = $2
			     AND source_chapter <= $3 AND valid_from_chapter <= $3
			 )
			 SELECT DISTINCT ON (f.attribute)
			        f.attribute, f.value, f.valid_from_chapter, f.source_chapter, f.confidence, COALESCE((SELECT jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash,'char_start',v.char_start,'char_end',v.char_end) FROM graph_evidence v WHERE v.id=f.evidence_id),'null'::jsonb)
			 FROM visible f
			 WHERE f.kind <> 'retraction'
			   AND NOT EXISTS (SELECT 1 FROM visible successor WHERE successor.supersedes = f.id)
			 ORDER BY f.attribute, f.valid_from_chapter DESC, f.source_chapter DESC,
			          f.confidence DESC, f.id DESC`, novelID, entityID, at)
		if err != nil {
			return err
		}
		defer factRows.Close()
		for factRows.Next() {
			var fact FactView
			if err := factRows.Scan(
				&fact.Attribute, &fact.Value, &fact.ValidFromChapter,
				&fact.SourceChapter, &fact.Confidence, &fact.Evidence,
			); err != nil {
				return err
			}
			view.Facts = append(view.Facts, fact)
		}
		return factRows.Err()
	})
	return view, err
}

func (s *Store) ListWiki(ctx context.Context, novelID string, at int) ([]EntitySummary, error) {
	entities := []EntitySummary{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx,
			`SELECT id::text, canonical, kind, first_seen_chapter
			 FROM entity
			 WHERE novel_id = $1 AND first_seen_chapter <= $2
			 ORDER BY first_seen_chapter, canonical, id`, novelID, at)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var entity EntitySummary
			if err := rows.Scan(
				&entity.ID, &entity.Canonical, &entity.Kind, &entity.FirstSeenChapter,
			); err != nil {
				return err
			}
			entities = append(entities, entity)
		}
		return rows.Err()
	})
	return entities, err
}

// ListGlossary gates on locked_at_chapter <= at, mirroring ListWiki's
// first_seen_chapter <= at pattern — a term locked at chapter 400 (e.g. proving a sect
// exists) is itself spoiler information. glossary carries no RLS policy (0002_rls.sql
// doesn't list it), so — unlike every other gated read here — this filter is app-layer
// only; still routed through withReaderTx for the same connection/role discipline as
// everything else, even though there's no inner RLS layer to back it up for this table.
func (s *Store) ListGlossary(ctx context.Context, novelID string, at int) ([]GlossaryTermView, error) {
	// A seed term can be linked to an entity discovered later. Only expose that ID
	// once the entity is authorized too; LEFT JOIN preserves the visible seed (§0.3).
	terms := []GlossaryTermView{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx,
			`SELECT g.source_term, g.target_term, g.version, g.locked_at_chapter, e.id::text
			 FROM glossary g
			 LEFT JOIN glossary_binding gb ON gb.novel_id=g.novel_id AND gb.source_term=g.source_term
             LEFT JOIN entity e ON e.id = COALESCE(gb.entity_id,g.entity_id) AND e.novel_id = g.novel_id
			   AND e.first_seen_chapter <= $2
			 WHERE g.novel_id = $1 AND g.locked_at_chapter <= $2 AND NOT g.deleted
			 ORDER BY g.source_term`, novelID, at)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var term GlossaryTermView
			if err := rows.Scan(
				&term.SourceTerm, &term.TargetTerm, &term.Version, &term.LockedAtChapter,
				&term.EntityID,
			); err != nil {
				return err
			}
			terms = append(terms, term)
		}
		return rows.Err()
	})
	return terms, err
}

func (s *Store) ListTimeline(ctx context.Context, novelID string, at int) ([]EventView, error) {
	events := []EventView{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		var err error
		events, err = listEventsInTx(ctx, tx, novelID, nil, at)
		return err
	})
	return events, err
}

// listEventsInTx reads the independently activated event ledger. RLS applies both the
// reader's knowledge-time cap and novel.active_event_revision (§0.1/§0.2). An argument's
// literal surface is always returned; its optional entity link is visible only when the
// linked entity is also authorized in the active graph revision.
func listEventsInTx(ctx context.Context, tx pgx.Tx, novelID string, chapter *int, at int) ([]EventView, error) {
	events := []EventView{}
	rows, err := tx.Query(ctx,
		`SELECT e.id::text,e.chapter_index,e.event_type,e.action,e.status,e.summary,e.result,
		        jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,
		          'source_hash',v.source_hash,'char_start',v.char_start,'char_end',v.char_end)
		 FROM chapter_event e JOIN event_evidence v ON v.id=e.evidence_id
		 WHERE e.novel_id=$1 AND e.chapter_index<=$3
		   AND ($2::int IS NULL OR e.chapter_index=$2)
		 ORDER BY e.chapter_index,e.id`, novelID, chapter, at)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		event := EventView{Arguments: []EventArgumentView{}, Entities: []EntitySummary{}}
		if err := rows.Scan(&event.ID, &event.ChapterIndex, &event.EventType, &event.Action,
			&event.Status, &event.Summary, &event.Result, &event.Evidence); err != nil {
			return nil, err
		}
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()

	for index := range events {
		argumentRows, err := tx.Query(ctx,
			`SELECT a.role,a.surface,visible.id::text,visible.canonical,visible.kind,visible.first_seen_chapter
			 FROM chapter_event_argument a
			 LEFT JOIN entity visible ON visible.id=a.entity_id
			   AND visible.revision_id=a.linked_graph_revision
			 WHERE a.event_id=$1 ORDER BY a.ordinal`, events[index].ID)
		if err != nil {
			return nil, err
		}
		seenEntities := map[string]bool{}
		for argumentRows.Next() {
			var argument EventArgumentView
			var canonical, kind *string
			var firstSeen *int
			if err := argumentRows.Scan(&argument.Role, &argument.Surface, &argument.EntityID,
				&canonical, &kind, &firstSeen); err != nil {
				argumentRows.Close()
				return nil, err
			}
			if argument.EntityID != nil && canonical != nil && kind != nil && firstSeen != nil {
				entity := EntitySummary{ID: *argument.EntityID, Canonical: *canonical, Kind: *kind, FirstSeenChapter: *firstSeen}
				argument.Entity = &entity
				if !seenEntities[entity.ID] {
					events[index].Entities = append(events[index].Entities, entity)
					seenEntities[entity.ID] = true
				}
			}
			events[index].Arguments = append(events[index].Arguments, argument)
		}
		if err := argumentRows.Err(); err != nil {
			argumentRows.Close()
			return nil, err
		}
		argumentRows.Close()
	}
	return events, nil
}

func (s *Store) ListRelationships(
	ctx context.Context, novelID, entityID string, at int,
) ([]RelationshipView, error) {
	relationships := []RelationshipView{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		var exists bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS (
			   SELECT 1 FROM entity
			   WHERE novel_id = $1 AND id = $2 AND first_seen_chapter <= $3
			 )`, novelID, entityID, at,
		).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return ErrNotFound
		}

		rows, err := tx.Query(ctx,
			`SELECT edge.id, edge.rel_type,
			        CASE WHEN edge.src_id = $2 THEN 'outgoing' ELSE 'incoming' END,
			        other.id::text, other.canonical, other.kind, other.first_seen_chapter,
			        edge.valid_from_chapter, edge.valid_to_chapter, edge.source_chapter, COALESCE((SELECT jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash,'char_start',v.char_start,'char_end',v.char_end) FROM graph_evidence v WHERE v.id=edge.evidence_id),'null'::jsonb)
			 FROM edge
			 JOIN entity other ON other.id = CASE
			   WHEN edge.src_id = $2 THEN edge.dst_id ELSE edge.src_id END
			 WHERE edge.novel_id = $1 AND (edge.src_id = $2 OR edge.dst_id = $2)
			   AND edge.source_chapter <= $3 AND edge.valid_from_chapter <= $3
			   AND (edge.valid_to_chapter IS NULL OR edge.valid_to_chapter > $3)
			   AND other.novel_id = $1 AND other.first_seen_chapter <= $3
			 ORDER BY edge.rel_type,
			          CASE WHEN edge.src_id = $2 THEN 'outgoing' ELSE 'incoming' END,
			          other.canonical, edge.id`, novelID, entityID, at)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var relationship RelationshipView
			if err := rows.Scan(
				&relationship.ID, &relationship.Relation, &relationship.Direction,
				&relationship.Entity.ID, &relationship.Entity.Canonical,
				&relationship.Entity.Kind, &relationship.Entity.FirstSeenChapter,
				&relationship.ValidFromChapter, &relationship.ValidToChapter,
				&relationship.SourceChapter, &relationship.Evidence,
			); err != nil {
				return err
			}
			relationships = append(relationships, relationship)
		}
		return rows.Err()
	})
	return relationships, err
}

func (s *Store) readObject(ctx context.Context, key string) (string, error) {
	obj, err := s.objects.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return "", err
	}
	defer obj.Close()
	body, err := io.ReadAll(obj)
	if err != nil {
		return "", err
	}
	return string(body), nil
}

// newFactsInTx loads the facts whose source_chapter is exactly this chapter — what the
// reader learns HERE, as opposed to everything they now know. It runs inside
// withReaderTx so the visible set is gated on reader_chapter() like every other read path
// (§0.2): being allowed to read chapter n is not on its own permission to see a fact
// carrying a later source_chapter, and this must not become the one path that assumes it.
//
// Gating is on source_chapter (knowledge-time). valid_from_chapter appears in the visible
// set only to match the entity card's display semantics — story-time never authorizes.
//
// Supersession and retraction are applied exactly as GetEntity applies them, so a fact
// introduced here but already overturned by a chapter the reader has since passed does not
// resurface as news. The alternative — badging it anyway — would contradict the entity
// card the badge links to.
func newFactsInTx(ctx context.Context, tx pgx.Tx, novelID string, n int, view *ChapterView) error {
	rows, err := tx.Query(ctx,
		`WITH visible AS (
		   SELECT id, entity_id, attribute, value, kind, supersedes,
		          valid_from_chapter, source_chapter, confidence
		   FROM fact
		   WHERE novel_id = $1
		     AND source_chapter <= reader_chapter() AND valid_from_chapter <= reader_chapter()
		 )
	 SELECT DISTINCT ON (f.entity_id, f.attribute)
	        f.entity_id::text, e.canonical, f.attribute, f.value, f.valid_from_chapter,
		        f.source_chapter, f.confidence
	 FROM visible f
	 JOIN entity e ON e.id = f.entity_id
		 WHERE f.source_chapter = $2
		   AND f.entity_id IS NOT NULL
		   AND f.kind <> 'retraction'
		   AND NOT EXISTS (SELECT 1 FROM visible successor WHERE successor.supersedes = f.id)
		 ORDER BY f.entity_id, f.attribute, f.valid_from_chapter DESC,
		          f.source_chapter DESC, f.confidence DESC, f.id DESC`,
		novelID, n)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var fact ChapterFactView
		if err := rows.Scan(&fact.EntityID, &fact.EntityCanonical, &fact.Attribute, &fact.Value,
			&fact.ValidFromChapter, &fact.SourceChapter, &fact.Confidence); err != nil {
			return err
		}
		view.NewFacts = append(view.NewFacts, fact)
	}
	return rows.Err()
}

// GetChapter serves the chapter body + display spans for the reader pane. The novel-id/
// progress cap is enforced by the caller (handler) before this is invoked; the span
// query below is additionally RLS-gated (novel_id/chapter_index <= reader_chapter())
// via withReaderTx, same defense-in-depth every other read gets.
func (s *Store) GetChapter(ctx context.Context, novelID string, n, at int) (ChapterView, error) {
	var rawURI, translatedURI, siteChapterNo, sourceURL *string
	var status string
	var warningCode *string
	var warningCount int
	var part int
	err := s.progressDB.QueryRow(ctx,
		`SELECT raw_uri, translated_uri, CASE WHEN translation_ready THEN 'done' ELSE status END, source_meta->>'site_chapter_no', source_meta->>'source_url',
		        COALESCE((source_meta->>'part')::int, 1),translation_warning_code,translation_warning_count
		 FROM chapter WHERE novel_id = $1 AND chapter_index = $2`,
		novelID, n,
	).Scan(&rawURI, &translatedURI, &status, &siteChapterNo, &sourceURL, &part, &warningCode, &warningCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return ChapterView{}, ErrNotFound
	}
	if err != nil {
		return ChapterView{}, err
	}
	if status != "done" {
		// Defensive belt-and-braces: progress can only ever advance to a chapter that
		// was `done` at the time (AdvanceProgress's own guard), so this shouldn't be
		// reachable in practice — but GetChapter is independently callable for any
		// n <= progress, so it must not trust that invariant blindly.
		return ChapterView{}, ErrChapterNotReady
	}

	key := translatedURI
	if key == nil {
		key = rawURI
	}
	text, err := s.readObject(ctx, *key)
	if err != nil {
		return ChapterView{}, err
	}

	view := ChapterView{Text: text, Spans: []SpanView{}, NewFacts: []ChapterFactView{}, Events: []EventView{}, Part: part}
	if warningCode != nil {
		view.TranslationWarning = &TranslationWarning{Code: *warningCode, TermCount: warningCount}
	}
	if siteChapterNo != nil {
		view.SiteChapterNo = *siteChapterNo
	}
	if sourceURL != nil {
		view.SourceURL = *sourceURL
	}
	if err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		var err error
		view.Knowledge, err = knowledgeInTx(ctx, tx, n)
		if err != nil {
			return err
		}
		view.EventKnowledge, err = eventKnowledgeInTx(ctx, tx, n)
		if err != nil {
			return err
		}
		view.Events, err = listEventsInTx(ctx, tx, novelID, &n, at)
		if err != nil {
			return err
		}
		rows, err := tx.Query(ctx,
			`SELECT d.id::text, e.id::text,d.char_start,d.char_end,b.known_from_chapter,d.mention_id::text, COALESCE((SELECT jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,'source_hash',v.source_hash) FROM graph_evidence v WHERE v.id=COALESCE(b.evidence_id,d.evidence_id)),'null'::jsonb)
             FROM display_mention d
             LEFT JOIN LATERAL (SELECT entity_id,known_from_chapter,evidence_id FROM mention_binding
               WHERE revision_id=d.revision_id AND mention_id=d.mention_id
               AND known_from_chapter<=reader_chapter() ORDER BY known_from_chapter DESC LIMIT 1) b ON true
             LEFT JOIN entity e ON e.id=b.entity_id
             WHERE d.novel_id=$1 AND d.chapter_index=$2
             UNION ALL
             SELECT 'legacy:'||m.id::text,e.id::text,m.char_start,m.char_end,NULL::int,NULL::text,'null'::jsonb
             FROM mention_span m LEFT JOIN entity e ON e.id=m.entity_id
             WHERE m.novel_id=$1 AND m.chapter_index=$2
               AND NOT EXISTS(SELECT 1 FROM display_mention d WHERE d.novel_id=$1 AND d.chapter_index=$2)
             ORDER BY 3`,
			novelID, n)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var span SpanView
			if err := rows.Scan(&span.MentionID, &span.EntityID, &span.CharStart, &span.CharEnd, &span.KnownFromChapter, &span.SourceMentionID, &span.Evidence); err != nil {
				return err
			}
			span.EnrichmentStatus = view.Knowledge.Status
			if span.EnrichmentStatus == "done" || span.EnrichmentStatus == "ready" {
				if span.EntityID == nil {
					span.EnrichmentStatus = "unresolved"
				} else {
					span.EnrichmentStatus = "linked"
				}
			}
			view.Spans = append(view.Spans, span)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if err := attachChapterRenderings(ctx, tx, novelID, n, at, text, view.Spans); err != nil {
			return err
		}
		return newFactsInTx(ctx, tx, novelID, n, &view)
	}); err != nil {
		return ChapterView{}, err
	}

	// "Has next" is an EXISTENCE check decoupled from the reader's own progress — a
	// linear reader finishing chapter n for the first time needs "Next" enabled once
	// n+1 exists, even when its translation is pending or failed. Content stays gated.
	view.HasNext, err = s.hasNextChapter(ctx, novelID, n)
	if err != nil {
		return ChapterView{}, err
	}

	return view, nil
}

// attachChapterRenderings makes terminology choices available even before RESOLVE has
// linked a display-only name to an entity. It joins reviews to their source occurrence in
// this exact chapter, then matches only the offered/locked target spellings to the literal
// display span. This is terminology association, never identity resolution: entity_id
// remains NULL and state.resolutions is still the only identity authority (§0.3, §12).
func attachChapterRenderings(
	ctx context.Context, tx pgx.Tx, novelID string, chapter, at int, text string, spans []SpanView,
) error {
	// The durable alignment is authoritative for terminology association. It is keyed by
	// the exact display offsets, so no reverse translation or entity-name matching occurs.
	aligned, err := tx.Query(ctx,
		`SELECT t.char_start,t.char_end,t.source_term,g.target_term,
		        CASE WHEN g.source_term IS NULL THEN 'unlocked' ELSE 'locked' END,
		        COALESCE(r.term_role,CASE WHEN g.constraint_class='character_name'
		          THEN 'chinese_person' ELSE 'semantic_term' END),
		        COALESCE(r.candidates,'[]'::jsonb)
		 FROM term_rendering_occurrence t
		 LEFT JOIN glossary g ON g.novel_id=t.novel_id AND g.source_term=t.source_term
		   AND g.locked_at_chapter <= $3 AND NOT g.deleted
		 LEFT JOIN character_name_review r ON r.novel_id=t.novel_id
		   AND r.source_term=t.source_term AND r.first_seen_chapter <= $3
		 WHERE t.novel_id=$1 AND t.chapter_index=$2`, novelID, chapter, at)
	if err != nil {
		return err
	}
	byOffset := map[[2]int]*TermRenderingView{}
	for aligned.Next() {
		var start, end int
		var rendering TermRenderingView
		var candidates []byte
		if err := aligned.Scan(&start, &end, &rendering.SourceTerm, &rendering.TargetTerm,
			&rendering.Status, &rendering.TermRole, &candidates); err != nil {
			aligned.Close()
			return err
		}
		if err := json.Unmarshal(candidates, &rendering.Candidates); err != nil {
			aligned.Close()
			return err
		}
		copy := rendering
		byOffset[[2]int{start, end}] = &copy
	}
	aligned.Close()
	if err := aligned.Err(); err != nil {
		return err
	}
	for index := range spans {
		if rendering := byOffset[[2]int{spans[index].CharStart, spans[index].CharEnd}]; rendering != nil {
			copy := *rendering
			spans[index].Rendering = &copy
		}
	}

	// Compatibility fallback for chapters not yet backfilled: reviewed spellings can be
	// associated without guessing by exact/normalized offered target spelling.
	rows, err := tx.Query(ctx,
		`SELECT DISTINCT r.source_term,g.target_term,
		        CASE WHEN g.source_term IS NULL THEN 'pending' ELSE 'locked' END,
		        r.term_role,r.candidates
		 FROM character_name_review r
		 JOIN character_name_occurrence o ON o.novel_id=r.novel_id
		   AND o.source_term=r.source_term AND o.chapter_index=$2
		 LEFT JOIN glossary g ON g.novel_id=r.novel_id AND g.source_term=r.source_term
		   AND g.locked_at_chapter <= $3 AND NOT g.deleted
		 WHERE r.novel_id=$1 AND r.first_seen_chapter <= $3
		   AND (r.status='pending' OR g.source_term IS NOT NULL)
		 ORDER BY r.source_term`, novelID, chapter, at)
	if err != nil {
		return err
	}
	defer rows.Close()

	bySpelling := map[string]*TermRenderingView{}
	for rows.Next() {
		var rendering TermRenderingView
		var candidates []byte
		if err := rows.Scan(&rendering.SourceTerm, &rendering.TargetTerm, &rendering.Status,
			&rendering.TermRole, &candidates); err != nil {
			return err
		}
		if err := json.Unmarshal(candidates, &rendering.Candidates); err != nil {
			return err
		}
		spellings := []string{}
		if rendering.TargetTerm != nil {
			spellings = append(spellings, *rendering.TargetTerm)
		}
		for _, candidate := range rendering.Candidates {
			spellings = append(spellings, candidate.TargetTerm)
		}
		copy := rendering
		for _, spelling := range spellings {
			key := normalizedRendering(spelling)
			if key == "" {
				continue
			}
			if previous, exists := bySpelling[key]; exists && previous != nil && previous.SourceTerm != rendering.SourceTerm {
				// An ambiguous spelling is not enough evidence to choose between two source
				// terms. Leave it unattached rather than offering the wrong correction.
				bySpelling[key] = nil
			} else if !exists {
				bySpelling[key] = &copy
			}
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}

	chars := []rune(text)
	for index := range spans {
		span := &spans[index]
		if span.Rendering != nil {
			continue
		}
		if span.CharStart < 0 || span.CharEnd > len(chars) || span.CharStart >= span.CharEnd {
			continue
		}
		if rendering := bySpelling[normalizedRendering(string(chars[span.CharStart:span.CharEnd]))]; rendering != nil {
			copy := *rendering
			span.Rendering = &copy
		}
	}
	return nil
}

func normalizedRendering(value string) string {
	var normalized strings.Builder
	for _, char := range strings.ToLower(value) {
		if unicode.IsLetter(char) || unicode.IsNumber(char) {
			normalized.WriteRune(char)
		}
	}
	return normalized.String()
}

// CreateScrapeJob inserts the job row and pushes its id onto the Redis queue the scraper
// service drains (PLAN.md Phase N5) — mirrors ingest-api's enqueue-a-pointer pattern for
// jobs:pending. The partial unique index scrape_job_one_active_per_novel (migration
// 0009) is what actually enforces "one active scrape per novel"; a violation surfaces
// here as ErrScrapeJobActive so the handler can map it to 409.
func (s *Store) CreateScrapeJob(ctx context.Context, novelID, startURL, mode string) (int64, error) {
	var id int64
	err := s.progressDB.QueryRow(ctx,
		`INSERT INTO scrape_job (novel_id, start_url, mode) VALUES ($1, $2, $3) RETURNING id`,
		novelID, startURL, mode,
	).Scan(&id)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return 0, ErrScrapeJobActive
		}
		return 0, err
	}
	if err := s.redis.LPush(ctx, scrapePendingQueue, id).Err(); err != nil {
		return 0, fmt.Errorf("enqueue scrape job: %w", err)
	}
	return id, nil
}

func (s *Store) LatestScrapeJob(ctx context.Context, novelID string) (ScrapeJobView, error) {
	var job ScrapeJobView
	err := s.progressDB.QueryRow(ctx,
		`SELECT id, novel_id::text, start_url, mode, status, chapters_fetched,
		        last_error, cancel_requested, created_at, updated_at
		 FROM scrape_job WHERE novel_id = $1 ORDER BY created_at DESC LIMIT 1`,
		novelID,
	).Scan(
		&job.ID, &job.NovelID, &job.StartURL, &job.Mode, &job.Status, &job.ChaptersFetched,
		&job.LastError, &job.CancelRequested, &job.CreatedAt, &job.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return ScrapeJobView{}, ErrNotFound
	}
	return job, err
}

// RequestScrapeCancel is a no-op (not an error) if no job is currently active — the
// scraper only ever checks cancel_requested on a job it's actively running, so setting
// it on a terminal job changes nothing observable.
func (s *Store) RequestScrapeCancel(ctx context.Context, novelID string) error {
	_, err := s.progressDB.Exec(ctx,
		`UPDATE scrape_job SET cancel_requested = true
		 WHERE novel_id = $1 AND status IN ('pending', 'running')`,
		novelID,
	)
	return err
}

var _ ReaderStore = (*Store)(nil)

func (s *Store) hasNextChapter(ctx context.Context, novelID string, chapter int) (bool, error) {
	var exists bool
	err := s.progressDB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM chapter WHERE novel_id=$1 AND chapter_index=$2)`, novelID, chapter+1).Scan(&exists)
	return exists, err
}
