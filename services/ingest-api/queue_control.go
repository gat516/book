package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
)

// Shared local-library controls; selecting a book only schedules already queued work.
// It never ingests chapters, advances reader progress, or interrupts an active claim.
const queueControlKey = "jobs:control"

// workerHeartbeatKey mirrors WORKER_HEARTBEAT in services/pipeline/pipeline/worker.py.
// The worker renews it from _heartbeat_forever, a task deliberately independent of the
// work loop, so it stays fresh through long inference calls and provider backoff alike.
// Presence is therefore the whole signal -- the key carries a TTL, so its existing at all
// means a worker checked in inside that window, and no clock is compared across services.
const workerHeartbeatKey = "jobs:worker:heartbeat"

type queueControlPatch struct {
	Mode         *string `json:"mode,omitempty"`
	FocusNovelID *string `json:"focus_novel_id,omitempty"`
	// These are operational labels, not authentication. reader-api supplies the reader
	// id it received so a shared-library pause can be explained to the next reader.
	ChangedBy string `json:"changed_by,omitempty"`
	Reason    string `json:"reason,omitempty"`
}

func (p *queueControlPatch) validate() error {
	if p.Mode == nil && p.FocusNovelID == nil {
		return fmt.Errorf("provide mode or focus_novel_id")
	}
	if p.Mode != nil && *p.Mode != "all" && *p.Mode != "focused" && *p.Mode != "paused" {
		return fmt.Errorf("mode must be all, focused, or paused")
	}
	if p.FocusNovelID != nil && *p.FocusNovelID != "" {
		id, err := uuid.Parse(*p.FocusNovelID)
		if err != nil {
			return fmt.Errorf("invalid focus_novel_id")
		}
		*p.FocusNovelID = id.String()
	}
	p.ChangedBy = strings.TrimSpace(p.ChangedBy)
	p.Reason = strings.TrimSpace(p.Reason)
	if len(p.ChangedBy) > 200 || len(p.Reason) > 500 {
		return fmt.Errorf("queue audit details are too long")
	}
	return nil
}

func (s *Store) applyQueueControl(ctx context.Context, key string, p queueControlPatch) error {
	fields := map[string]interface{}{}
	if p.Mode != nil {
		fields["mode"] = *p.Mode
		fields["mode_changed_at"] = time.Now().UTC().Format(time.RFC3339)
		fields["mode_changed_by"] = p.ChangedBy
		fields["mode_reason"] = p.Reason
	}
	if p.FocusNovelID != nil {
		fields["focus_novel_id"] = *p.FocusNovelID
	}
	// One HSET is atomic and preserves omitted fields: focusing must not unpause.
	return s.redis.HSet(ctx, key, fields).Err()
}

type queueChapter struct {
	ChapterIndex int    `json:"chapter_index"`
	Stage        string `json:"stage"`
}
type queueBook struct {
	NovelID  string         `json:"novel_id"`
	Title    string         `json:"title"`
	Pending  int            `json:"pending"`
	InFlight []queueChapter `json:"in_flight"`
}
type queueControlResponse struct {
	Mode          string       `json:"mode"`
	FocusNovelID  string       `json:"focus_novel_id"`
	ModeChangedAt string       `json:"mode_changed_at,omitempty"`
	ModeChangedBy string       `json:"mode_changed_by,omitempty"`
	ModeReason    string       `json:"mode_reason,omitempty"`
	WorkerAlive   bool         `json:"worker_alive"`
	Books         []*queueBook `json:"books"`
}

func (s *Store) queueControl(ctx context.Context) (queueControlResponse, error) {
	pipe := s.redis.TxPipeline()
	settings := pipe.HGetAll(ctx, queueControlKey)
	pending := pipe.LRange(ctx, pendingQueue, 0, -1)
	processing := pipe.LRange(ctx, "jobs:processing", 0, -1)
	stages := pipe.HGetAll(ctx, "jobs:processing:stage")
	// Exists, not Get: a missing key is the answer here, and Get would return redis.Nil
	// and fail the whole pipeline -- reporting a dead worker as an unreachable queue,
	// which is the one substitution this endpoint must never make.
	workerAlive := pipe.Exists(ctx, workerHeartbeatKey)
	if _, err := pipe.Exec(ctx); err != nil {
		return queueControlResponse{}, err
	}
	result := queueControlResponse{
		Mode: settings.Val()["mode"], FocusNovelID: settings.Val()["focus_novel_id"],
		ModeChangedAt: settings.Val()["mode_changed_at"], ModeChangedBy: settings.Val()["mode_changed_by"],
		ModeReason: settings.Val()["mode_reason"], Books: []*queueBook{},
		WorkerAlive: workerAlive.Val() == 1,
	}
	if result.Mode == "" {
		result.Mode = "all"
	}
	titles := map[string]string{}
	rows, err := s.db.Query(ctx, "SELECT id::text, title FROM novel")
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		var id, title string
		if err := rows.Scan(&id, &title); err != nil {
			return result, err
		}
		titles[id] = title
	}
	if err := rows.Err(); err != nil {
		return result, err
	}
	books := map[string]*queueBook{}
	book := func(id string) *queueBook {
		if books[id] == nil {
			title, ok := titles[id]
			if !ok {
				title = "Deleted book"
			}
			books[id] = &queueBook{NovelID: id, Title: title, InFlight: []queueChapter{}}
		}
		return books[id]
	}
	if result.FocusNovelID != "" {
		book(result.FocusNovelID)
	}
	for _, raw := range pending.Val() {
		var msg QueueMessage
		if json.Unmarshal([]byte(raw), &msg) == nil && msg.NovelID != "" {
			book(msg.NovelID).Pending++
		}
	}
	for _, raw := range processing.Val() {
		var msg QueueMessage
		if json.Unmarshal([]byte(raw), &msg) == nil && msg.NovelID != "" {
			b := book(msg.NovelID)
			b.InFlight = append(b.InFlight, queueChapter{ChapterIndex: msg.ChapterIndex, Stage: stages.Val()[raw]})
		}
	}
	for _, b := range books {
		result.Books = append(result.Books, b)
	}
	sort.Slice(result.Books, func(i, j int) bool {
		a, b := result.Books[i], result.Books[j]
		if (a.NovelID == result.FocusNovelID) != (b.NovelID == result.FocusNovelID) {
			return a.NovelID == result.FocusNovelID
		}
		if a.Title != b.Title {
			return a.Title < b.Title
		}
		return a.NovelID < b.NovelID
	})
	return result, nil
}

func (a *API) queueControl(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPatch {
		var patch queueControlPatch
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&patch); err != nil {
			writeErr(w, 400, "invalid queue settings")
			return
		}
		if err := patch.validate(); err != nil {
			writeErr(w, 400, err.Error())
			return
		}
		if patch.FocusNovelID != nil && *patch.FocusNovelID != "" {
			var exists bool
			err := a.store.db.QueryRow(r.Context(), "SELECT EXISTS(SELECT 1 FROM novel WHERE id=$1)", *patch.FocusNovelID).Scan(&exists)
			if err != nil {
				writeErr(w, 500, "could not check book")
				return
			}
			if !exists {
				writeErr(w, 404, "book not found")
				return
			}
		}
		if err := a.store.applyQueueControl(r.Context(), queueControlKey, patch); err != nil {
			log.Printf("queue control update: %v", err)
			writeErr(w, 500, "could not update queue settings")
			return
		}
	}
	result, err := a.store.queueControl(r.Context())
	if err != nil {
		log.Printf("queue control: %v", err)
		writeErr(w, 500, "could not read queue")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, 200, result)
}
