package main

import (
	"context"
	"fmt"
	"log"
	"net/http"

	"github.com/minio/minio-go/v7"
	"github.com/redis/go-redis/v9"
)

// scrapePendingQueue must match services/scraper/job.go's pendingQueue constant (and the
// key reader-api enqueues onto in its own store.go).
const scrapePendingQueue = "scrape:pending"

// purgePendingScript drops every queued chapter pointer belonging to one novel. Same shape
// as prioritizeScript (priority.go): the queue holds JSON pointers, so matching means
// decoding each element, and doing that inside Redis keeps it one atomic pass instead of a
// read-modify-write race against the worker.
var purgePendingScript = redis.NewScript(`
local removed = 0
for _, raw in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
  local ok, m = pcall(cjson.decode, raw)
  if ok and type(m) == 'table' and m.novel_id == ARGV[1] then
    removed = removed + redis.call('LREM', KEYS[1], 0, raw)
  end
end
if redis.call('HGET', KEYS[2], 'focus_novel_id') == ARGV[1] then
  redis.call('HDEL', KEYS[2], 'focus_novel_id')
end
return removed
`)

// deleteNovel removes a novel and everything derived from it. Migration 0030 made every
// foreign key cascade, so the single DELETE below is the whole database operation — the
// ordering of ~29 tables lives in the schema, not here, where it would rot. Reports false
// if no such novel existed (handler maps that to 404).
//
// Postgres is the source of truth; Redis and the object store are cleaned up afterwards on
// a best-effort basis. A failure there leaves orphaned bytes, not a half-deleted novel, so
// it is logged rather than returned — and re-running the delete would 404 without helping.
func (s *Store) deleteNovel(ctx context.Context, novelID string) (bool, error) {
	// Read the scrape job ids before the row goes: scrape:pending holds bare job ids, so
	// after the cascade there is no way left to tell which queued ids were this novel's.
	rows, err := s.db.Query(ctx, `SELECT id FROM scrape_job WHERE novel_id = $1`, novelID)
	if err != nil {
		return false, fmt.Errorf("list scrape jobs: %w", err)
	}
	var scrapeJobIDs []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return false, fmt.Errorf("scan scrape job id: %w", err)
		}
		scrapeJobIDs = append(scrapeJobIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return false, fmt.Errorf("list scrape jobs: %w", err)
	}

	tag, err := s.db.Exec(ctx, `DELETE FROM novel WHERE id = $1`, novelID)
	if err != nil {
		return false, fmt.Errorf("delete novel: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return false, nil
	}

	s.purgeQueues(ctx, novelID, scrapeJobIDs)
	s.purgeObjects(ctx, novelID)
	return true, nil
}

// purgeQueues drops the novel's queued work from both Redis queues. Work already claimed
// by the pipeline stays owned until its deletion watcher cancels the model call and
// releases the claim and preview. Do not remove active claims here before cancellation
// has unwound. A scrape mid-flight still fails on its next database write.
func (s *Store) purgeQueues(ctx context.Context, novelID string, scrapeJobIDs []int64) {
	if _, err := purgePendingScript.Run(ctx, s.redis, []string{pendingQueue, queueControlKey}, novelID).Int(); err != nil {
		log.Printf("deleteNovel %s: purge %s: %v", novelID, pendingQueue, err)
	}
	for _, id := range scrapeJobIDs {
		if err := s.redis.LRem(ctx, scrapePendingQueue, 0, id).Err(); err != nil {
			log.Printf("deleteNovel %s: purge %s job %d: %v", novelID, scrapePendingQueue, id, err)
		}
	}
}

// purgeObjects removes the novel's chapter bodies. The key scheme putRawObject builds is
// novels/{id}/… precisely so a novel's objects can be swept by prefix like this.
func (s *Store) purgeObjects(ctx context.Context, novelID string) {
	prefix := fmt.Sprintf("novels/%s/", novelID)
	objects := s.minio.ListObjects(ctx, s.bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true})
	for err := range s.minio.RemoveObjects(ctx, s.bucket, objects, minio.RemoveObjectsOptions{}) {
		log.Printf("deleteNovel %s: remove object %s: %v", novelID, err.ObjectName, err.Err)
	}
}

// deleteNovel handles DELETE /novels/{id}. Token-gated like POST /novels: those two are the
// destructive novel-lifecycle routes reader-api proxies for the browser, and this one is
// irreversible, so it does not get the "writer service, no auth here" treatment the
// chapter/glossary routes do.
func (a *API) deleteNovel(w http.ResponseWriter, r *http.Request) {
	novelID := r.PathValue("id")
	deleted, err := a.store.deleteNovel(r.Context(), novelID)
	if err != nil {
		log.Printf("deleteNovel: %v", err)
		writeErr(w, http.StatusInternalServerError, "could not delete novel")
		return
	}
	if !deleted {
		writeErr(w, http.StatusNotFound, "no such novel")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": true})
}
