package main

import (
	"context"
	"encoding/json"

	"github.com/redis/go-redis/v9"
)

// §6.3: workers claim from the RIGHT. Move only this pointer, atomically with the
// processing-list check, so repeated clicks cannot duplicate an active chapter.
var prioritizeScript = redis.NewScript(`
local function matches(raw)
  local ok, m = pcall(cjson.decode, raw)
  return ok and type(m) == 'table' and m.novel_id == ARGV[1]
    and tonumber(m.chapter_index) == tonumber(ARGV[2])
end
for _, raw in ipairs(redis.call('LRANGE', KEYS[2], 0, -1)) do
  if matches(raw) then return 0 end
end
for _, raw in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
  if matches(raw) then
    redis.call('LREM', KEYS[1], 0, raw)
  end
end
-- Explicit priority must survive the worker's chapter-number scheduling.
redis.call('RPUSH', KEYS[1], ARGV[3])
return 1
`)

func (s *Store) prioritizeChapter(ctx context.Context, novelID string, chapter int) (bool, error) {
	var status string
	if err := s.db.QueryRow(ctx, `SELECT status FROM chapter WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter).Scan(&status); err != nil {
		return false, err
	}
	if status == "done" {
		return false, nil
	}
	if _, err := s.queueTranslationRange(ctx, novelID, chapter, 1); err != nil {
		return false, err
	}
	// Re-read after queueing: another worker may have finished while we waited.
	if err := s.db.QueryRow(ctx, `SELECT status FROM chapter WHERE novel_id=$1 AND chapter_index=$2`, novelID, chapter).Scan(&status); err != nil {
		return false, err
	}
	if status != "queued" {
		return false, nil
	}
	payload, err := json.Marshal(QueueMessage{NovelID: novelID, ChapterIndex: chapter, Priority: true})
	if err != nil {
		return false, err
	}
	result, err := prioritizeScript.Run(ctx, s.redis, []string{pendingQueue, "jobs:processing"}, novelID, chapter, string(payload)).Int()
	return result == 1, err
}
