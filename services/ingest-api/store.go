package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/minio/minio-go/v7"
	"github.com/redis/go-redis/v9"
)

// pendingQueue is the Redis list the pipeline drains (instructions.md §13.2 scales workers
// on its depth). Keep this name in sync with the KEDA ScaledObject listName.
const pendingQueue = "jobs:pending"

// Store bundles the three backing clients and provides the small set of data-access
// helpers the handlers need. Nothing here contains business logic — it is the seam
// between HTTP handling and the stores.
type Store struct {
	db     *pgxpool.Pool
	redis  *redis.Client
	minio  *minio.Client
	bucket string
}

// insertNovel creates a novel row with the resolved ontology and returns its generated id.
// providerConfig is optional (PLAN.md Phase N3); when present, the novel row and its
// novel_provider_config row are inserted in one transaction — a half-written provider
// config is worse than failing novel creation entirely.
func (s *Store) insertNovel(
	ctx context.Context, title, sourceLang, targetLang, genre string, ont Ontology,
	providerConfig *ProviderConfigInput,
) (string, error) {
	ontJSON, err := json.Marshal(ont)
	if err != nil {
		return "", fmt.Errorf("marshal ontology: %w", err)
	}
	// genre is nullable in the schema; store NULL rather than "" so auto-induction (§4.2)
	// can distinguish "no genre given" from a real value later.
	var genreArg any
	if genre != "" {
		genreArg = genre
	}

	tx, err := s.db.Begin(ctx)
	if err != nil {
		return "", fmt.Errorf("begin: %w", err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()

	var id string
	if err := tx.QueryRow(ctx,
		`INSERT INTO novel (title, source_lang, target_lang, genre, ontology)
		 VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		title, sourceLang, targetLang, genreArg, ontJSON,
	).Scan(&id); err != nil {
		return "", fmt.Errorf("insert novel: %w", err)
	}

	if providerConfig != nil {
		if err := insertProviderConfig(ctx, tx, id, *providerConfig); err != nil {
			return "", fmt.Errorf("insert provider config: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return "", fmt.Errorf("commit: %w", err)
	}
	return id, nil
}

// getNovelSourceLang returns the novel's source language, used to fill the envelope.
// Returns pgx.ErrNoRows if the novel does not exist (handler maps that to 404).
func (s *Store) getNovelSourceLang(ctx context.Context, novelID string) (string, error) {
	var lang string
	err := s.db.QueryRow(ctx, `SELECT source_lang FROM novel WHERE id = $1`, novelID).Scan(&lang)
	return lang, err
}

// chapterIndexByHash reports whether this novel already holds a chapter with exactly this
// body, and at which chapter_index (migration 0013). This is the content-addressed half of
// "ingestion is idempotent, content-hash keyed" (instructions.md §0) — raw_hash was
// recorded from the very first migration but never actually keyed on, which is how a
// re-run scrape silently re-ingested 27 chapters under fresh indices.
//
// Cheap by construction: the hash is already computed for every paste regardless (it is
// the cache key), and 0013's unique index makes this an index probe rather than a scan.
func (s *Store) chapterIndexByHash(ctx context.Context, novelID, rawHash string) (int, bool, error) {
	var chapterIndex int
	err := s.db.QueryRow(ctx,
		`SELECT chapter_index FROM chapter WHERE novel_id = $1 AND raw_hash = $2`,
		novelID, rawHash,
	).Scan(&chapterIndex)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("lookup chapter by hash: %w", err)
	}
	return chapterIndex, true, nil
}

// queueTranslationRange marks every still-unqueued chapter in [from, from+count) as
// 'queued' and pushes a pipeline job for each, returning the indices actually queued.
//
// This is the demand-driven half of translation (migration 0015). Ingestion no longer
// queues what it accepts, because fetching outruns translating by orders of magnitude and
// queueing everything buried the reader's own chapters behind hundreds nobody was reading.
//
// The UPDATE ... WHERE status = 'ingested' RETURNING is what makes this safe to call
// repeatedly and concurrently: only the caller that actually flips a row observes it, so a
// chapter is queued exactly once no matter how many readers ask. Chapters already queued,
// done, or errored are skipped by the same predicate.
func (s *Store) queueTranslationRange(ctx context.Context, novelID string, from, count int) ([]int, error) {
	if count <= 0 {
		return []int{}, nil
	}
	// 'error' is re-queueable, not terminal. A chapter fails for reasons that are often
	// external and fixable — a model that mangles terminology, a provider outage, a
	// timeout — so refusing to retry it would strand it permanently, including after the
	// operator switches to a provider that would succeed. 'ingested' and 'error' are the
	// two states with no pending work; 'queued' and 'done' are excluded so repeat calls
	// stay no-ops and finished chapters are never redone.
	rows, err := s.db.Query(ctx,
		`UPDATE chapter SET status = 'queued'
		 WHERE novel_id = $1 AND chapter_index >= $2 AND chapter_index < $3
		   AND status IN ('ingested', 'error')
		 RETURNING chapter_index`,
		novelID, from, from+count,
	)
	if err != nil {
		return nil, fmt.Errorf("mark chapters queued: %w", err)
	}
	queued := []int{}
	for rows.Next() {
		var index int
		if err := rows.Scan(&index); err != nil {
			rows.Close()
			return nil, err
		}
		queued = append(queued, index)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}

	for _, index := range queued {
		if err := s.enqueue(ctx, QueueMessage{NovelID: novelID, ChapterIndex: index}); err != nil {
			// The row is already marked 'queued', so returning here would strand it: it
			// would never be re-queued by a later call. Put it back so the next request
			// retries it.
			_, _ = s.db.Exec(ctx,
				`UPDATE chapter SET status = 'ingested'
				 WHERE novel_id = $1 AND chapter_index = $2 AND status = 'queued'`,
				novelID, index)
			return nil, fmt.Errorf("enqueue chapter %d: %w", index, err)
		}
	}
	return queued, nil
}

// novelLookahead returns how many chapters past the reader this novel keeps translated.
func (s *Store) novelLookahead(ctx context.Context, novelID string) (int, error) {
	var lookahead int
	err := s.db.QueryRow(ctx,
		`SELECT translate_lookahead FROM novel WHERE id = $1`, novelID,
	).Scan(&lookahead)
	return lookahead, err
}

// putRawObject uploads a chapter body to the object store and returns its key (raw_uri).
// The key is deterministic so re-pasting the same chapter overwrites identical bytes
// rather than accumulating duplicates (idempotency, §0.7).
func (s *Store) putRawObject(ctx context.Context, novelID string, chapterIndex int, raw string) (string, error) {
	key := fmt.Sprintf("novels/%s/chapters/%d/raw.txt", novelID, chapterIndex)
	body := []byte(raw)
	_, err := s.minio.PutObject(ctx, s.bucket, key, bytes.NewReader(body), int64(len(body)),
		minio.PutObjectOptions{ContentType: "text/plain; charset=utf-8"})
	if err != nil {
		return "", fmt.Errorf("put object: %w", err)
	}
	return key, nil
}

// putTranslatedObject mirrors putRawObject's key scheme, under translated/ instead of
// chapters/.../raw.txt (PLAN.md N5/N6 half-translated bootstrap).
func (s *Store) putTranslatedObject(ctx context.Context, novelID string, chapterIndex int, translated string) (string, error) {
	key := fmt.Sprintf("novels/%s/chapters/%d/translated.txt", novelID, chapterIndex)
	body := []byte(translated)
	_, err := s.minio.PutObject(ctx, s.bucket, key, bytes.NewReader(body), int64(len(body)),
		minio.PutObjectOptions{ContentType: "text/plain; charset=utf-8"})
	if err != nil {
		return "", fmt.Errorf("put translated object: %w", err)
	}
	return key, nil
}

// insertChapter writes the chapter row. Idempotent: re-pasting the same (novel, index)
// is a no-op on the row (ON CONFLICT DO NOTHING) so ingestion can be safely retried.
// translatedURI/translatedBy are "" for a normal paste (source == target, or translation
// happens later via the pipeline) and set together only for a pre-translated chapter.
func (s *Store) insertChapter(ctx context.Context, env ChapterEnvelope, rawURI, translatedURI, translatedBy string) error {
	metaJSON, err := json.Marshal(env.SourceMeta)
	if err != nil {
		return fmt.Errorf("marshal source_meta: %w", err)
	}
	var translatedURIArg, translatedByArg any
	if translatedURI != "" {
		translatedURIArg, translatedByArg = translatedURI, translatedBy
	}
	_, err = s.db.Exec(ctx,
		`INSERT INTO chapter (novel_id, chapter_index, raw_hash, raw_uri, source_meta, status, translated_uri, translated_by)
		 VALUES ($1, $2, $3, $4, $5, 'ingested', $6, $7)
		 ON CONFLICT (novel_id, chapter_index) DO NOTHING`,
		env.NovelID, env.ChapterIndex, env.SourceMeta.RawHash, rawURI, metaJSON, translatedURIArg, translatedByArg,
	)
	if err != nil {
		return fmt.Errorf("insert chapter: %w", err)
	}
	return nil
}

// enqueue LPUSHes a lightweight pointer onto the pending queue for the pipeline to pick
// up. We push a pointer (not the body) so Redis stays small; the pipeline reads the body
// from the object store via chapter.raw_uri.
func (s *Store) enqueue(ctx context.Context, msg QueueMessage) error {
	payload, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal queue message: %w", err)
	}
	if err := s.redis.LPush(ctx, pendingQueue, payload).Err(); err != nil {
		return fmt.Errorf("lpush: %w", err)
	}
	return nil
}
