package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/google/uuid"
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
)

type ReaderStore interface {
	Health(context.Context) error
	GetProgress(context.Context, string, string) (Progress, error)
	AdvanceProgress(context.Context, string, string, int) (Progress, error)
	GetEntity(context.Context, string, string, int) (EntityView, error)
	ListWiki(context.Context, string, int) ([]EntitySummary, error)
	ListTimeline(context.Context, string, int) ([]EventView, error)
	ListRelationships(context.Context, string, string, int) ([]RelationshipView, error)
	GetChapter(context.Context, string, int) (ChapterView, error)
	ListChapters(context.Context, string, int, int) ([]ChapterListItem, int, error)
	PipelineStatus(context.Context, string) (PipelineStatusResponse, error)
	ListNovels(context.Context) ([]NovelSummary, error)
	GetNovel(context.Context, string) (NovelSummary, error)
	CreateScrapeJob(context.Context, string, string, string) (int64, error)
	LatestScrapeJob(context.Context, string) (ScrapeJobView, error)
	RequestScrapeCancel(context.Context, string) error
	ListGlossary(context.Context, string, int) ([]GlossaryTermView, error)
}

type Store struct {
	readerDB   *pgxpool.Pool
	progressDB *pgxpool.Pool
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
	readerDB, err := newRolePool(ctx, cfg.ReaderDatabaseURL, readerRole)
	if err != nil {
		return nil, err
	}
	progressDB, err := newRolePool(ctx, cfg.ProgressDatabaseURL, progressRole)
	if err != nil {
		readerDB.Close()
		return nil, err
	}
	return &Store{
		readerDB:   readerDB,
		progressDB: progressDB,
		objects:    objects,
		bucket:     cfg.ObjectBucket,
		redis:      redisClient,
	}, nil
}

func (s *Store) Close() {
	s.readerDB.Close()
	s.progressDB.Close()
}

func (s *Store) Health(ctx context.Context) error {
	if err := s.readerDB.Ping(ctx); err != nil {
		return fmt.Errorf("reader database: %w", err)
	}
	if err := s.progressDB.Ping(ctx); err != nil {
		return fmt.Errorf("progress database: %w", err)
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
		 WHERE c.novel_id = $2 AND c.chapter_index = $3 AND c.status = 'done'
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
		`SELECT chapter_index, source_meta->>'site_chapter_no',
		        COALESCE((source_meta->>'part')::int, 1), status
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
		var siteChapterNo *string
		if err := rows.Scan(&item.ChapterIndex, &siteChapterNo, &item.Part, &item.Status); err != nil {
			return nil, 0, err
		}
		if siteChapterNo != nil {
			item.SiteChapterNo = *siteChapterNo
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

	pending, err := s.redis.LLen(ctx, pipelinePendingQueue).Result()
	if err != nil {
		return PipelineStatusResponse{}, fmt.Errorf("pending queue depth: %w", err)
	}
	status.Pending = int(pending)

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
		status.InFlight = append(status.InFlight, item)
	}
	return status, nil
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
	tx, err := s.readerDB.Begin(ctx)
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
	view := EntityView{Aliases: []string{}, Facts: []FactView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
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

		factRows, err := tx.Query(ctx,
			`WITH visible AS (
			   SELECT id, attribute, value, kind, supersedes, valid_from_chapter,
			          source_chapter, confidence
			   FROM fact
			   WHERE novel_id = $1 AND entity_id = $2
			     AND source_chapter <= $3 AND valid_from_chapter <= $3
			 )
			 SELECT DISTINCT ON (f.attribute)
			        f.attribute, f.value, f.valid_from_chapter, f.source_chapter, f.confidence
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
				&fact.SourceChapter, &fact.Confidence,
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
	terms := []GlossaryTermView{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx,
			`SELECT source_term, target_term, version, locked_at_chapter
			 FROM glossary
			 WHERE novel_id = $1 AND locked_at_chapter <= $2
			 ORDER BY source_term`, novelID, at)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var term GlossaryTermView
			if err := rows.Scan(
				&term.SourceTerm, &term.TargetTerm, &term.Version, &term.LockedAtChapter,
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
		rows, err := tx.Query(ctx,
			`SELECT id, chapter_index, summary, entity_ids
			 FROM event
			 WHERE novel_id = $1 AND chapter_index <= $2
			 ORDER BY chapter_index, id`, novelID, at)
		if err != nil {
			return err
		}
		type eventRow struct {
			event EventView
			ids   []uuid.UUID
		}
		buffered := []eventRow{}
		for rows.Next() {
			row := eventRow{event: EventView{Entities: []EntitySummary{}}}
			if err := rows.Scan(
				&row.event.ID, &row.event.ChapterIndex, &row.event.Summary, &row.ids,
			); err != nil {
				rows.Close()
				return err
			}
			buffered = append(buffered, row)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()

		for _, row := range buffered {
			entityRows, err := tx.Query(ctx,
				`SELECT e.id::text, e.canonical, e.kind, e.first_seen_chapter
				 FROM unnest($1::uuid[]) WITH ORDINALITY AS ref(id, ordinal)
				 JOIN entity e ON e.id = ref.id
				 WHERE e.novel_id = $2 AND e.first_seen_chapter <= $3
				 ORDER BY ref.ordinal`, row.ids, novelID, at)
			if err != nil {
				return err
			}
			for entityRows.Next() {
				var entity EntitySummary
				if err := entityRows.Scan(
					&entity.ID, &entity.Canonical, &entity.Kind, &entity.FirstSeenChapter,
				); err != nil {
					entityRows.Close()
					return err
				}
				row.event.Entities = append(row.event.Entities, entity)
			}
			if err := entityRows.Err(); err != nil {
				entityRows.Close()
				return err
			}
			entityRows.Close()
			events = append(events, row.event)
		}
		return nil
	})
	return events, err
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
			        edge.valid_from_chapter, edge.valid_to_chapter, edge.source_chapter
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
				&relationship.SourceChapter,
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

// GetChapter serves the chapter body + display spans for the reader pane. The novel-id/
// progress cap is enforced by the caller (handler) before this is invoked; the span
// query below is additionally RLS-gated (novel_id/chapter_index <= reader_chapter())
// via withReaderTx, same defense-in-depth every other read gets.
func (s *Store) GetChapter(ctx context.Context, novelID string, n int) (ChapterView, error) {
	var rawURI, translatedURI, siteChapterNo *string
	var status string
	var part int
	err := s.progressDB.QueryRow(ctx,
		`SELECT raw_uri, translated_uri, status, source_meta->>'site_chapter_no',
		        COALESCE((source_meta->>'part')::int, 1)
		 FROM chapter WHERE novel_id = $1 AND chapter_index = $2`,
		novelID, n,
	).Scan(&rawURI, &translatedURI, &status, &siteChapterNo, &part)
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

	view := ChapterView{Text: text, Spans: []SpanView{}, Part: part}
	if siteChapterNo != nil {
		view.SiteChapterNo = *siteChapterNo
	}
	if err := s.withReaderTx(ctx, novelID, n, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx,
			`SELECT entity_id::text, char_start, char_end FROM mention_span
			 WHERE novel_id = $1 AND chapter_index = $2 ORDER BY char_start`,
			novelID, n)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var span SpanView
			if err := rows.Scan(&span.EntityID, &span.CharStart, &span.CharEnd); err != nil {
				return err
			}
			view.Spans = append(view.Spans, span)
		}
		return rows.Err()
	}); err != nil {
		return ChapterView{}, err
	}

	// "Has next" is an EXISTENCE check decoupled from the reader's own progress — a
	// linear reader finishing chapter n for the first time needs "Next" enabled once
	// n+1 exists and is processed, not only once progress has already passed it.
	if err := s.progressDB.QueryRow(ctx,
		`SELECT EXISTS (
		   SELECT 1 FROM chapter WHERE novel_id = $1 AND chapter_index = $2 AND status = 'done'
		 )`, novelID, n+1,
	).Scan(&view.HasNext); err != nil {
		return ChapterView{}, err
	}

	return view, nil
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
