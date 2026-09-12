package main

// Reader-side records contracts. Every query runs inside withReaderTx, so the chapter
// GUCs the RLS policies read are set and the database refuses future rows even if a
// predicate here were wrong (§13.1). The explicit `source_chapter <= at` predicates are
// the second, independent layer, not the only one.

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// recordsStatusFor reports extraction/rendering progress for one chapter (chapter != nil)
// or for everything the reader may see (chapter == nil).
//
// The version is bounded by the reader's own chapter cap: publishing chapter 40 must not
// change the token a chapter-3 reader holds, or every reader's cache would churn as the
// backfill advances.
func recordsStatusFor(ctx context.Context, tx pgx.Tx, novelID string, chapter *int, at int) (string, RecordsStatus, error) {
	var generation, extraction, rendering string
	var version int64
	var warnings int
	var detail *string
	var retryAttempts, enrichmentAttempts int
	var retryAt *time.Time
	var retryCategory *string
	err := tx.QueryRow(ctx, `
SELECT g.id::text,
  COALESCE((SELECT CASE WHEN EXISTS (
                          SELECT 1
                            FROM chapter c
                           WHERE c.novel_id=$1
                             AND ((c.enrichment_retry_at IS NOT NULL AND c.enrichment_attempts < 5)
                               OR (c.provider_retry_at IS NOT NULL AND c.provider_retry_attempts < 5))
                             AND c.chapter_index <= $2
                             AND ($3::int IS NULL OR c.chapter_index = $3::int)
                        ) THEN 'processing'
                        WHEN bool_or(r.status='failed') THEN 'failed'
                        WHEN EXISTS (
                          SELECT 1
                            FROM chapter c
                            JOIN chapter_failure cf
                              ON cf.novel_id=c.novel_id AND cf.chapter_index=c.chapter_index
                           WHERE c.novel_id=$1
                             AND ((c.provider_retry_attempts >= 5 AND c.provider_retry_at IS NULL
                                   AND cf.error_code='provider_retry_exhausted')
                               OR (c.enrichment_attempts >= 5 AND c.enrichment_retry_at IS NULL
                                   AND cf.error_code <> 'provider_retry_exhausted'))
                             AND c.chapter_index <= $2
                             AND ($3::int IS NULL OR c.chapter_index = $3::int)
                        ) THEN 'failed'
                        WHEN bool_or(r.status='processing') THEN 'processing'
                        WHEN count(*) > 0 AND bool_and(r.status='published') THEN 'ready'
                        ELSE 'pending' END
              FROM record_run r
             WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index <= $2
               AND ($3::int IS NULL OR r.chapter_index = $3::int)),
             CASE WHEN EXISTS (
                       SELECT 1
                         FROM chapter c
                        WHERE c.novel_id=$1
                          AND ((c.enrichment_retry_at IS NOT NULL AND c.enrichment_attempts < 5)
                            OR (c.provider_retry_at IS NOT NULL AND c.provider_retry_attempts < 5))
                          AND c.chapter_index <= $2
                          AND ($3::int IS NULL OR c.chapter_index = $3::int)
                    ) THEN 'processing'
                    WHEN EXISTS (
                       SELECT 1
                         FROM chapter c
                         JOIN chapter_failure cf
                           ON cf.novel_id=c.novel_id AND cf.chapter_index=c.chapter_index
                        WHERE c.novel_id=$1
                          AND ((c.provider_retry_attempts >= 5 AND c.provider_retry_at IS NULL
                                AND cf.error_code='provider_retry_exhausted')
                            OR (c.enrichment_attempts >= 5 AND c.enrichment_retry_at IS NULL
                                AND cf.error_code <> 'provider_retry_exhausted'))
                          AND c.chapter_index <= $2
                          AND ($3::int IS NULL OR c.chapter_index = $3::int)
                    ) THEN 'failed' ELSE 'pending' END),
  COALESCE((SELECT CASE WHEN bool_or(rr.status='failed') THEN 'failed'
                        WHEN bool_and(rr.status='ready') THEN 'ready'
                        ELSE 'pending' END
              FROM record_rendering rr
              JOIN record_row w ON w.id=rr.row_id
             WHERE w.novel_id=$1 AND w.generation_id=g.id AND w.source_chapter <= $2
               AND ($3::int IS NULL OR w.source_chapter = $3::int)), 'ready'),
  COALESCE((SELECT max(r.publication_version) FROM record_run r
             WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index <= $2), 0),
  COALESCE((SELECT sum(r.warning_count) FROM record_run r
             WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index <= $2
               AND ($3::int IS NULL OR r.chapter_index = $3::int)), 0),
  COALESCE((SELECT r.diagnostics->>'failure' FROM record_run r
    WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.status='failed' AND r.chapter_index <= $2
      AND ($3::int IS NULL OR r.chapter_index = $3::int)
    ORDER BY r.chapter_index DESC LIMIT 1),
  (SELECT cf.error_code FROM chapter c
     JOIN chapter_failure cf
       ON cf.novel_id=c.novel_id AND cf.chapter_index=c.chapter_index
    WHERE c.novel_id=$1
      AND ((c.provider_retry_attempts >= 5 AND c.provider_retry_at IS NULL
            AND cf.error_code='provider_retry_exhausted')
        OR (c.enrichment_attempts >= 5 AND c.enrichment_retry_at IS NULL
            AND cf.error_code <> 'provider_retry_exhausted'))
      AND c.chapter_index <= $2
      AND ($3::int IS NULL OR c.chapter_index = $3::int)
	    ORDER BY c.chapter_index, cf.occurred_at DESC, cf.id DESC LIMIT 1)),
  COALESCE((SELECT max(GREATEST(c.provider_retry_attempts,c.enrichment_attempts)) FROM chapter c
    WHERE c.novel_id=$1 AND c.chapter_index <= $2
      AND ($3::int IS NULL OR c.chapter_index=$3::int)),0),
  (SELECT min(retry_at) FROM (SELECT c.provider_retry_at AS retry_at FROM chapter c
	    WHERE c.novel_id=$1 AND ($3::int IS NULL OR c.chapter_index=$3::int)
	    UNION ALL SELECT c.enrichment_retry_at FROM chapter c
	    WHERE c.novel_id=$1 AND ($3::int IS NULL OR c.chapter_index=$3::int)) retries),
  (SELECT c.provider_retry_category FROM chapter c
    WHERE c.novel_id=$1 AND ($3::int IS NULL OR c.chapter_index=$3::int)
    ORDER BY c.chapter_index LIMIT 1),
  COALESCE((SELECT c.enrichment_attempts FROM chapter c
    WHERE c.novel_id=$1 AND ($3::int IS NULL OR c.chapter_index=$3::int)
    ORDER BY c.chapter_index LIMIT 1),0)
  FROM novel n JOIN record_generation g ON g.id=n.active_record_generation
 WHERE n.id=$1`, novelID, at, chapter).
		Scan(&generation, &extraction, &rendering, &version, &warnings, &detail,
			&retryAttempts, &retryAt, &retryCategory, &enrichmentAttempts)
	if errors.Is(err, pgx.ErrNoRows) {
		// No active generation yet: the novel exists, its knowledge does not.
		return "", RecordsStatus{Version: "none:0", ExtractionStatus: "pending", RenderingStatus: "pending"}, nil
	}
	if err != nil {
		return "", RecordsStatus{}, err
	}
	if extraction != "ready" && rendering == "ready" {
		rendering = "pending"
	}
	if enrichmentAttempts > retryAttempts {
		retryAttempts = enrichmentAttempts
	}
	// A failed run with a future durable retry is not terminal: the worker will retry
	// it without a user action. Provider exhaustion has no retry_at and remains failed.
	if extraction == "failed" && retryAt != nil && retryAt.After(time.Now()) &&
		(detail == nil || *detail != "provider_retry_exhausted") {
		extraction = "pending"
	}
	return generation, RecordsStatus{
		GenerationID:     generation,
		Version:          fmt.Sprintf("%s:%d", generation, version),
		ExtractionStatus: extraction,
		RenderingStatus:  rendering,
		WarningCount:     warnings,
		FailureDetail:    detail,
		RetryAttempts:    retryAttempts,
		RetryMaxAttempts: 5,
		RetryAt:          retryAt,
		RetryCategory:    retryCategory,
	}, nil
}

// hydrateRows fills each row's values, participants and evidence. The base query decides
// which rows are visible; this only expands them.
func hydrateRows(ctx context.Context, tx pgx.Tx, base pgx.Rows) ([]RecordView, error) {
	out := []RecordView{}
	for base.Next() {
		var row RecordView
		if err := base.Scan(&row.ID, &row.Type, &row.OriginalIndex, &row.SourceChapter,
			&row.ValidFromChapter, &row.TemporalQualifier); err != nil {
			base.Close()
			return nil, err
		}
		row.Values = []RecordValueView{}
		row.Participants = []RecordParticipantView{}
		row.Evidence = []RecordEvidenceView{}
		out = append(out, row)
	}
	base.Close()
	if err := base.Err(); err != nil {
		return nil, err
	}
	for i := range out {
		row := &out[i]
		vals, err := tx.Query(ctx, `SELECT v.field_name,v.source_value,COALESCE(r.target_value,''),COALESCE(r.status,'pending')
			FROM record_value v
			LEFT JOIN record_rendering r ON r.row_id=v.row_id AND r.field_name=v.field_name
			WHERE v.row_id=$1 ORDER BY v.field_name`, row.ID)
		if err != nil {
			return nil, err
		}
		for vals.Next() {
			var v RecordValueView
			if err := vals.Scan(&v.Field, &v.Source, &v.Rendered, &v.RenderStatus); err != nil {
				vals.Close()
				return nil, err
			}
			row.Values = append(row.Values, v)
		}
		vals.Close()
		ev, err := tx.Query(ctx, `SELECT e.passage_id,e.quote,p.text,p.char_start,p.char_end,p.ordinal
			FROM record_evidence e
			JOIN record_passage p ON p.run_id=e.run_id AND p.passage_id=e.passage_id
			WHERE e.row_id=$1 ORDER BY p.ordinal`, row.ID)
		if err != nil {
			return nil, err
		}
		for ev.Next() {
			var e RecordEvidenceView
			if err := ev.Scan(&e.PassageID, &e.Quote, &e.Text, &e.CharStart, &e.CharEnd, &e.Ordinal); err != nil {
				ev.Close()
				return nil, err
			}
			row.Evidence = append(row.Evidence, e)
		}
		ev.Close()
		parts, err := tx.Query(ctx, `SELECT field_name,surface,entity_id::text,reference_id::text
			FROM record_participant WHERE row_id=$1 ORDER BY ordinal`, row.ID)
		if err != nil {
			return nil, err
		}
		for parts.Next() {
			var p RecordParticipantView
			if err := parts.Scan(&p.Field, &p.Surface, &p.EntityID, &p.ReferenceID); err != nil {
				parts.Close()
				return nil, err
			}
			row.Participants = append(row.Participants, p)
		}
		parts.Close()
	}
	return out, nil
}

func (s *Store) ListRecords(ctx context.Context, novelID string, chapter, at int) (RecordsResponse, error) {
	out := RecordsResponse{NovelID: novelID, ChapterIndex: chapter, At: at, Rows: []RecordView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		if chapter > at {
			return ErrNotFound
		}
		generation, status, err := recordsStatusFor(ctx, tx, novelID, &chapter, at)
		if err != nil {
			return err
		}
		out.Status = status
		if generation == "" {
			return nil
		}
		base, err := tx.Query(ctx, `SELECT id::text,record_type,original_index,source_chapter,valid_from_chapter,temporal_qualifier
			FROM record_row WHERE novel_id=$1 AND generation_id=$2 AND source_chapter=$3
			ORDER BY original_index LIMIT 500`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		out.Rows, err = hydrateRows(ctx, tx, base)
		return err
	})
	return out, err
}

// ListTimeline orders by knowledge chapter and then passage order within the chapter.
// A record recounting something older keeps its temporal qualifier so the UI can label
// it; story time itself is unknown unless the source established it.
func (s *Store) ListTimeline(ctx context.Context, novelID string, at int) (TimelineResponse, error) {
	out := TimelineResponse{NovelID: novelID, At: at, Rows: []RecordView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		generation, status, err := recordsStatusFor(ctx, tx, novelID, nil, at)
		if err != nil {
			return err
		}
		out.Status = status
		if generation == "" {
			return nil
		}
		base, err := tx.Query(ctx, `SELECT w.id::text,w.record_type,w.original_index,w.source_chapter,w.valid_from_chapter,w.temporal_qualifier
			FROM record_row w
			WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter <= $3
			  AND w.record_type IN ('EVENT','SPEECH','PROMISE')
			ORDER BY w.source_chapter,
			         COALESCE((SELECT min(p.ordinal) FROM record_evidence e
			                    JOIN record_passage p ON p.run_id=e.run_id AND p.passage_id=e.passage_id
			                   WHERE e.row_id=w.id), 0),
			         w.original_index
			LIMIT 500`, novelID, generation, at)
		if err != nil {
			return err
		}
		out.Rows, err = hydrateRows(ctx, tx, base)
		return err
	})
	return out, err
}

func (s *Store) ListWiki(ctx context.Context, novelID string, at int) (WikiResponse, error) {
	out := WikiResponse{NovelID: novelID, At: at, Entities: []EntitySummary{}, Rows: []RecordView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		generation, status, err := recordsStatusFor(ctx, tx, novelID, nil, at)
		if err != nil {
			return err
		}
		out.Status = status
		if generation == "" {
			return nil
		}
		ents, err := tx.Query(ctx, `SELECT id::text,canonical,kind,first_seen_chapter FROM entity
			WHERE novel_id=$1 AND record_generation_id=$2 AND first_seen_chapter <= $3
			ORDER BY first_seen_chapter,canonical LIMIT 500`, novelID, generation, at)
		if err != nil {
			return err
		}
		for ents.Next() {
			var e EntitySummary
			if err := ents.Scan(&e.ID, &e.Canonical, &e.Kind, &e.FirstSeenChapter); err != nil {
				ents.Close()
				return err
			}
			out.Entities = append(out.Entities, e)
		}
		ents.Close()
		if err := ents.Err(); err != nil {
			return err
		}
		base, err := tx.Query(ctx, `SELECT id::text,record_type,original_index,source_chapter,valid_from_chapter,temporal_qualifier
			FROM record_row WHERE novel_id=$1 AND generation_id=$2 AND source_chapter <= $3 AND record_type='IDENTITY'
			ORDER BY source_chapter,original_index LIMIT 500`, novelID, generation, at)
		if err != nil {
			return err
		}
		out.Rows, err = hydrateRows(ctx, tx, base)
		return err
	})
	return out, err
}

// GetEntity returns one identity with the records it takes part in. Aliases carry their
// own first_seen_chapter: a name revealed in chapter 40 stays invisible to a chapter-3
// reader even though the entity itself is visible.
func (s *Store) GetEntity(ctx context.Context, novelID, entityID string, at int) (EntityResponse, error) {
	out := EntityResponse{NovelID: novelID, At: at}
	out.Entity.Aliases = []string{}
	out.Entity.Records = []RecordView{}
	out.Entity.Renderings = []TermRenderingView{}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		generation, _, err := recordsStatusFor(ctx, tx, novelID, nil, at)
		if err != nil {
			return err
		}
		if generation == "" {
			return ErrNotFound
		}
		err = tx.QueryRow(ctx, `SELECT id::text,canonical,kind,first_seen_chapter FROM entity
			WHERE id=$1 AND novel_id=$2 AND record_generation_id=$3 AND first_seen_chapter <= $4`,
			entityID, novelID, generation, at).
			Scan(&out.Entity.ID, &out.Entity.Canonical, &out.Entity.Kind, &out.Entity.FirstSeenChapter)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		aliases, err := tx.Query(ctx, `SELECT surface FROM alias
			WHERE entity_id=$1 AND record_generation_id=$2 AND first_seen_chapter <= $3
			ORDER BY first_seen_chapter,surface`, entityID, generation, at)
		if err != nil {
			return err
		}
		for aliases.Next() {
			var surface string
			if err := aliases.Scan(&surface); err != nil {
				aliases.Close()
				return err
			}
			out.Entity.Aliases = append(out.Entity.Aliases, surface)
		}
		aliases.Close()
		if err := aliases.Err(); err != nil {
			return err
		}
		base, err := tx.Query(ctx, `SELECT DISTINCT w.id::text,w.record_type,w.original_index,w.source_chapter,w.valid_from_chapter,w.temporal_qualifier
			FROM record_row w JOIN record_participant p ON p.row_id=w.id
			WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter <= $3 AND p.entity_id=$4
			ORDER BY w.source_chapter DESC,w.original_index LIMIT 200`, novelID, generation, at, entityID)
		if err != nil {
			return err
		}
		if out.Entity.Records, err = hydrateRows(ctx, tx, base); err != nil {
			return err
		}
		// Terminology the reader may already have seen rendered in prose. Wording is a
		// glossary decision, kept separate from identity: these are the terms whose
		// source form this entity answers to, not a claim that the term names it.
		terms, err := tx.Query(ctx, `SELECT g.source_term,g.target_term,g.locked_at_chapter
			FROM glossary g
			WHERE g.novel_id=$1 AND NOT g.deleted AND g.locked_at_chapter <= $2
			  AND g.source_term IN (SELECT surface FROM alias WHERE entity_id=$3
			                         AND record_generation_id=$4 AND first_seen_chapter <= $2)
			ORDER BY g.source_term`, novelID, at, entityID, generation)
		if err != nil {
			return err
		}
		for terms.Next() {
			var view TermRenderingView
			var locked int
			if err := terms.Scan(&view.SourceTerm, &view.TargetTerm, &locked); err != nil {
				terms.Close()
				return err
			}
			view.Status = "locked"
			view.Candidates = []CharacterNameCandidate{}
			out.Entity.Renderings = append(out.Entity.Renderings, view)
		}
		terms.Close()
		return terms.Err()
	})
	return out, err
}

// ListRecordsInspector is the operator view of one chapter's extraction: what the model
// produced, what the deterministic checks rejected and why, and what identity could not
// be resolved. It is capped at the reader's chapter like every other read.
func (s *Store) ListRecordsInspector(ctx context.Context, novelID string, chapter, at int) (RecordsInspectorResponse, error) {
	out := RecordsInspectorResponse{NovelID: novelID, ChapterIndex: chapter, At: at, Drops: []RecordDropView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		if chapter > at {
			return ErrNotFound
		}
		generation, status, err := recordsStatusFor(ctx, tx, novelID, &chapter, at)
		if err != nil {
			return err
		}
		out.Status = status
		if generation == "" {
			return nil
		}
		if err := tx.QueryRow(ctx, `SELECT
			(SELECT count(*) FROM record_row w WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter=$3),
			(SELECT count(*) FROM record_drop d JOIN record_run r ON r.id=d.run_id
			  WHERE d.novel_id=$1 AND d.generation_id=$2 AND r.chapter_index=$3),
			(SELECT count(*) FROM record_reference f JOIN record_run r ON r.id=f.run_id
			  WHERE f.novel_id=$1 AND f.generation_id=$2 AND r.chapter_index=$3),
			(SELECT count(*) FROM record_rendering g JOIN record_row w ON w.id=g.row_id
			  WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter=$3 AND g.status='failed')`,
			novelID, generation, chapter).
			Scan(&out.Retained, &out.Dropped, &out.Unresolved, &out.RenderingFailures); err != nil {
			return err
		}
		out.Parsed = out.Retained + out.Dropped
		drops, err := tx.Query(ctx, `SELECT COALESCE(d.original_index,-1),d.reasons
			FROM record_drop d JOIN record_run r ON r.id=d.run_id
			WHERE d.novel_id=$1 AND d.generation_id=$2 AND r.chapter_index=$3
			ORDER BY d.original_index LIMIT 200`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		for drops.Next() {
			var view RecordDropView
			if err := drops.Scan(&view.OriginalIndex, &view.Reasons); err != nil {
				drops.Close()
				return err
			}
			out.Drops = append(out.Drops, view)
		}
		drops.Close()
		return drops.Err()
	})
	return out, err
}

func (a *API) getRecords(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	out, err := a.store.ListRecords(r.Context(), novel, chapter, at)
	a.writeRecords(w, out, err)
}

func (a *API) getRecordsInspector(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	out, err := a.store.ListRecordsInspector(r.Context(), novel, chapter, at)
	a.writeRecords(w, out, err)
}

func (a *API) getWiki(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	out, err := a.store.ListWiki(r.Context(), novel, at)
	a.writeRecords(w, out, err)
}

func (a *API) getTimeline(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	out, err := a.store.ListTimeline(r.Context(), novel, at)
	a.writeRecords(w, out, err)
}

func (a *API) getEntity(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	entityID, ok := pathUUID(r, "eid")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid entity id")
		return
	}
	out, err := a.store.GetEntity(r.Context(), novel, entityID, at)
	a.writeRecords(w, out, err)
}

func (a *API) writeRecords(w http.ResponseWriter, out any, err error) {
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load records")
		return
	}
	writeJSON(w, http.StatusOK, out)
}

// Records maintenance actions. Reader-api never writes knowledge itself; these proxy to
// ingest-api behind its internal token, the same path every other mutation takes.
func (a *API) postRecordsAction(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	chapter := r.PathValue("n")
	action := "rebuild"
	switch {
	case chapter == "":
		action = "rebuild"
	case strings.HasSuffix(r.URL.Path, "/render-retry"):
		action = "render-retry"
	default:
		action = "retry"
	}
	result, status, err := a.ingest.RecordsAction(r.Context(), novelID, chapter, action)
	if err != nil {
		log.Printf("records %s: %v", action, err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
