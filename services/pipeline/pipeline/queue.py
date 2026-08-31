"""Atomic chapter scheduling and renewable claims (spec §5.1, §6.3).

Preserve FIFO between novels, but choose the lowest queued chapter within the
oldest eligible novel. The focused book comes first, followed by explicit chapter
requests. Pause/focused-only controls retain ineligible pointers. Only one chapter
of a novel may be in flight, including across multiple workers.
"""

PENDING = "jobs:pending"
PROCESSING = "jobs:processing"
STARTED = "jobs:processing:started"
STAGE = "jobs:processing:stage"
HEARTBEAT = "jobs:processing:heartbeat"
CONTROL = "jobs:control"  # HASH: mode (all|focused|paused), focus_novel_id
KEYS = [PENDING, PROCESSING, STARTED, STAGE, HEARTBEAT, CONTROL]

CLAIM = """
local mode = redis.call('HGET', KEYS[6], 'mode') or 'all'
local focus = redis.call('HGET', KEYS[6], 'focus_novel_id') or ''
if mode == 'paused' then return nil end
local function decode(raw)
  local ok, m = pcall(cjson.decode, raw)
  if ok and type(m) == 'table' and type(m.novel_id) == 'string'
      and tonumber(m.chapter_index) then return m end
end
local function rank(m)
  return (m.novel_id == focus and 4 or 0)
       + (m.priority == true and 2 or 0)
       + (m.enrichment ~= true and 1 or 0)
end
local active = {}
for _, raw in ipairs(redis.call('LRANGE', KEYS[2], 0, -1)) do
  local m = decode(raw)
  if m then active[m.novel_id] = true end
end
local pending = redis.call('LRANGE', KEYS[1], 0, -1)
local selected, selected_msg
for i = #pending, 1, -1 do
  local raw = pending[i]
  local m = decode(raw)
  if not m then
    -- Let the worker diagnose malformed pointers without stranding the queue.
    if not selected then selected = raw end
  elseif not active[m.novel_id] and (mode ~= 'focused' or m.novel_id == focus) then
    if not selected_msg then
      selected, selected_msg = raw, m
    elseif rank(m) > rank(selected_msg)
        or (rank(m) == rank(selected_msg) and m.priority ~= true
        and m.novel_id == selected_msg.novel_id
        and tonumber(m.chapter_index) < tonumber(selected_msg.chapter_index)) then
      selected, selected_msg = raw, m
    end
  end
end
if not selected then return nil end
-- Collapse alternate JSON encodings/priority copies of the same pointer.
for _, raw in ipairs(pending) do
  local m = decode(raw)
  if raw == selected or (selected_msg and m and m.novel_id == selected_msg.novel_id
      and tonumber(m.chapter_index) == tonumber(selected_msg.chapter_index)) then
    redis.call('LREM', KEYS[1], 0, raw)
  end
end
redis.call('LPUSH', KEYS[2], selected)
redis.call('HSET', KEYS[3], selected, ARGV[1])
redis.call('HSET', KEYS[5], selected, ARGV[1])
return selected
"""

RENEW = """
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[5], ARGV[1], ARGV[3])
return 1
"""

RELEASE = """
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('LREM', KEYS[2], 0, ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
if ARGV[3] == 'retry' then redis.call('LPUSH', KEYS[1], ARGV[1]) end
return 1
"""

REAP = """
local started = redis.call('HGET', KEYS[3], ARGV[1])
if not started then return 0 end
local alive = redis.call('HGET', KEYS[5], ARGV[1]) or started
if tonumber(ARGV[2]) - tonumber(alive) < tonumber(ARGV[3]) then return 0 end
local removed = redis.call('LREM', KEYS[2], 0, ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
if removed > 0 then redis.call('LPUSH', KEYS[1], ARGV[1]) end
return removed
"""

ENQUEUE_ENRICHMENT = """
for _, key in ipairs({KEYS[1], KEYS[2]}) do
  for _, raw in ipairs(redis.call('LRANGE', key, 0, -1)) do
    local ok, m = pcall(cjson.decode, raw)
    if ok and type(m) == 'table' and m.novel_id == ARGV[1]
        and tonumber(m.chapter_index) == tonumber(ARGV[2]) then return 0 end
  end
end
redis.call('LPUSH', KEYS[1], ARGV[3])
return 1
"""
