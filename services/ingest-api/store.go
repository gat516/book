package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

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
func (s *Store) insertNovel(ctx context.Context, title, sourceLang, targetLang, genre string, ont Ontology) (string, error) {
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
	var id string
	err = s.db.QueryRow(ctx,
		`INSERT INTO novel (title, source_lang, target_lang, genre, ontology)
		 VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		title, sourceLang, targetLang, genreArg, ontJSON,
	).Scan(&id)
	if err != nil {
		return "", fmt.Errorf("insert novel: %w", err)
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

// insertChapter writes the chapter row. Idempotent: re-pasting the same (novel, index)
// is a no-op on the row (ON CONFLICT DO NOTHING) so ingestion can be safely retried.
func (s *Store) insertChapter(ctx context.Context, env ChapterEnvelope, rawURI string) error {
	metaJSON, err := json.Marshal(env.SourceMeta)
	if err != nil {
		return fmt.Errorf("marshal source_meta: %w", err)
	}
	_, err = s.db.Exec(ctx,
		`INSERT INTO chapter (novel_id, chapter_index, raw_hash, raw_uri, source_meta, status)
		 VALUES ($1, $2, $3, $4, $5, 'ingested')
		 ON CONFLICT (novel_id, chapter_index) DO NOTHING`,
		env.NovelID, env.ChapterIndex, env.SourceMeta.RawHash, rawURI, metaJSON,
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
