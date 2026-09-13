package main

// Reader-side records contracts. Every query runs inside withReaderTx, so the chapter
// GUCs the RLS policies read are set and the database refuses future rows even if a
// predicate here were wrong (§13.1). The explicit `source_chapter <= at` predicates are
// the second, independent layer, not the only one.

import (
	"context"
	"encoding/json"
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
	var discarded bool
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
  CASE WHEN NOT EXISTS (
              SELECT 1 FROM record_row w0
              JOIN record_run r0 ON r0.id=w0.run_id AND r0.status='published'
             WHERE w0.novel_id=$1 AND w0.generation_id=g.id AND w0.source_chapter <= $2
               AND ($3::int IS NULL OR w0.source_chapter = $3::int)
       ) THEN 'ready'
       ELSE COALESCE((SELECT CASE WHEN bool_or(rr.status='failed') THEN 'failed'
                                  WHEN bool_and(rr.status='ready') THEN 'ready'
                                  ELSE 'pending' END
                        FROM record_rendering rr
                        JOIN record_row w ON w.id=rr.row_id
                       WHERE w.novel_id=$1 AND w.generation_id=g.id AND w.source_chapter <= $2
                         AND ($3::int IS NULL OR w.source_chapter = $3::int)), 'pending')
       END,
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
    ORDER BY c.chapter_index LIMIT 1),0),
  -- Deliberately answers only for a named chapter. A discarded chapter has no run row,
  -- so its extraction reads as 'pending' and the reader is told work is queued when the
  -- queue is empty and nothing will ever claim it. Aggregated over a whole book this
  -- would instead report one paused chapter as a paused library, so it stays false there.
  COALESCE((SELECT c.enrichment_discarded FROM chapter c
    WHERE c.novel_id=$1 AND $3::int IS NOT NULL AND c.chapter_index=$3::int),false)
  FROM novel n JOIN record_generation g ON g.id=n.active_record_generation
 WHERE n.id=$1`, novelID, at, chapter).
		Scan(&generation, &extraction, &rendering, &version, &warnings, &detail,
			&retryAttempts, &retryAt, &retryCategory, &enrichmentAttempts, &discarded)
	if errors.Is(err, pgx.ErrNoRows) {
		// No active generation yet: the novel exists, its knowledge does not.
		return "", RecordsStatus{Version: "none:0", ExtractionStatus: "pending", RenderingStatus: "pending"}, nil
	}
	if err != nil {
		return "", RecordsStatus{}, err
	}
	var waitingOn *int
	if chapter != nil {
		if err := tx.QueryRow(ctx, `SELECT min(c.chapter_index) FROM chapter c
			WHERE c.novel_id=$1 AND c.chapter_index < $2
			  AND c.translation_ready
			  AND NOT EXISTS (SELECT 1 FROM record_run r
			        WHERE r.novel_id=c.novel_id AND r.generation_id=$3::uuid
			          AND r.chapter_index=c.chapter_index AND r.status='published')`,
			novelID, *chapter, generation).Scan(&waitingOn); err != nil {
			return "", RecordsStatus{}, err
		}
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
	status := RecordsStatus{
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
		Discarded:        discarded,
		WaitingOnChapter: waitingOn,
	}
	if err := factFirstStatus(ctx, tx, novelID, generation, chapter, &status); err != nil {
		return "", RecordsStatus{}, err
	}
	return generation, status, nil
}

// factFirstStatus enriches the common status contract from the run-scoped audit tables.
// It is a no-op before migration 0104, keeping old generations readable during rollout.
func factFirstStatus(ctx context.Context, tx pgx.Tx, novelID, generation string, chapter *int, status *RecordsStatus) error {
	var raw []byte
	err := tx.QueryRow(ctx, `SELECT reader_fact_first_status($1,$2::uuid,$3)`, novelID, generation, chapter).Scan(&raw)
	if err != nil {
		return err
	}
	var projection struct {
		Discovered    int               `json:"discovered"`
		Selected      int               `json:"selected"`
		Omitted       int               `json:"omitted"`
		Consolidated  int               `json:"consolidated"`
		Rejected      int               `json:"rejected"`
		Unrepresented int               `json:"unrepresented"`
		Published     int               `json:"published"`
		Stages        map[string]string `json:"stages"`
	}
	if err := json.Unmarshal(raw, &projection); err != nil {
		return err
	}
	counts := KnowledgeCounts{Discovered: projection.Discovered, Selected: projection.Selected, Omitted: projection.Omitted, Consolidated: projection.Consolidated, Rejected: projection.Rejected, Unrepresented: projection.Unrepresented, Published: projection.Published}
	status.Counts = &counts
	status.Stages = projection.Stages
	if status.Stages == nil {
		status.Stages = map[string]string{}
	}
	// A book-level status is an aggregate, never one arbitrary chapter's diagnostic.
	// Fill missing stage detail from the aggregate extraction/rendering state so all six
	// stages have deterministic meaning while per-run diagnostics remain available.
	if chapter == nil {
		stageState := status.ExtractionStatus
		if stageState == "ready" {
			stageState = "completed"
		}
		for _, name := range []string{"discovery", "selection", "normalization", "identity_resolution", "publication"} {
			if _, ok := status.Stages[name]; !ok {
				status.Stages[name] = stageState
			}
		}
		if _, ok := status.Stages["rendering"]; !ok {
			state := status.RenderingStatus
			if state == "ready" {
				state = "completed"
			}
			status.Stages["rendering"] = state
		}
	}
	// Native rendering diagnostics are stage-scoped. Reflect the visible stage in the
	// legacy top-level field while retaining the SQL-derived value when no native stage
	// is available (for older generations).
	if renderingStage, ok := status.Stages["rendering"]; ok {
		switch renderingStage {
		case "completed", "ready":
			status.RenderingStatus = "ready"
		case "failed":
			status.RenderingStatus = "failed"
		case "processing":
			status.RenderingStatus = "processing"
		default:
			status.RenderingStatus = "pending"
		}
	}
	if counts.Discovered == 0 {
		status.SelectionOutcome = "empty"
	} else if counts.Selected == 0 {
		status.SelectionOutcome = "empty"
	} else if counts.Selected > 0 && counts.Published == 0 && counts.Rejected > 0 {
		status.SelectionOutcome = "all_rejected"
	} else {
		status.SelectionOutcome = "selected"
	}
	return nil
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
		if err != nil {
			return err
		}
		// Fact-first runs are the native extraction contract. Keep the legacy record
		// query above for old generations, then append native rows when present; the
		// run/generation/source predicates and RLS enforce the same spoiler boundary.
		native, err := factFirstRows(ctx, tx, novelID, generation, chapter, at, "")
		if err != nil {
			return err
		}
		out.Rows = append(out.Rows, native...)
		return nil
	})
	return out, err
}

func factFirstRows(ctx context.Context, tx pgx.Tx, novelID, generation string, chapter, at int, entityID string) ([]RecordView, error) {
	query := `WITH native AS (
 SELECT f.id::text id, r.id::text run_id, 'FACT' type, f.source_chapter, f.valid_from_chapter,
        f.temporal temporal_qualifier, f.polarity, f.attribution, f.condition, f.source_value,
        f.attribute label, f.value, NULL::text action, NULL::jsonb arguments,
        jsonb_build_array(jsonb_build_object('field','subject','surface',COALESCE(ep.source_name,ref.surface,f.subject_ref),
          'entity_id',ep.persistent_entity_id,'reference_id',CASE WHEN ep.persistent_entity_id IS NULL THEN ref.reference_id END)) participants,
        jsonb_build_array(jsonb_build_object('field',f.attribute,'source',f.value,'rendered',rr.target_value,'render_status',COALESCE(rr.status,'pending'))) value_json,
        f.evidence evidence, row_number() OVER (ORDER BY f.source_chapter,f.local_id,f.id) original_index
   FROM fact_first_fact f JOIN fact_first_run r ON r.id=f.run_id
   LEFT JOIN fact_first_entity_proposal ep ON ep.run_id=f.run_id AND ep.proposal_id=f.subject_ref
   LEFT JOIN fact_first_reference ref ON ref.run_id=f.run_id AND ref.reference_id=f.subject_ref
   LEFT JOIN fact_first_rendering rr ON rr.run_id=f.run_id AND rr.assertion_id=f.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=f.local_id
  WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.status='published' AND f.source_chapter<= $4
    AND (($3 >= 0 AND r.chapter_index=$3) OR ($3 < 0 AND r.chapter_index <= $4))
 UNION ALL
 SELECT f.id::text,r.id::text,'RELATION',f.source_chapter,f.valid_from_chapter,f.temporal,f.polarity,f.attribution,f.condition,f.source_value,
        f.relation,NULL,NULL,NULL,
        jsonb_build_array(jsonb_build_object('field','source','surface',COALESCE(es.source_name,rs.surface,f.src_ref),'entity_id',es.persistent_entity_id,'reference_id',CASE WHEN es.persistent_entity_id IS NULL THEN rs.reference_id END),
                          jsonb_build_object('field','target','surface',COALESCE(ed.source_name,rd.surface,f.dst_ref),'entity_id',ed.persistent_entity_id,'reference_id',CASE WHEN ed.persistent_entity_id IS NULL THEN rd.reference_id END)),
        jsonb_build_array(jsonb_build_object('field','relation','source',f.relation,'rendered',rr.target_value,'render_status',COALESCE(rr.status,'pending'))), f.evidence,
        row_number() OVER (ORDER BY f.source_chapter,f.local_id,f.id)
   FROM fact_first_relation f JOIN fact_first_run r ON r.id=f.run_id
   LEFT JOIN fact_first_entity_proposal es ON es.run_id=f.run_id AND es.proposal_id=f.src_ref
   LEFT JOIN fact_first_entity_proposal ed ON ed.run_id=f.run_id AND ed.proposal_id=f.dst_ref
   LEFT JOIN fact_first_reference rs ON rs.run_id=f.run_id AND rs.reference_id=f.src_ref
   LEFT JOIN fact_first_reference rd ON rd.run_id=f.run_id AND rd.reference_id=f.dst_ref
   LEFT JOIN fact_first_rendering rr ON rr.run_id=f.run_id AND rr.assertion_id=f.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=f.local_id
  WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.status='published' AND f.source_chapter<=$4
    AND (($3 >= 0 AND r.chapter_index=$3) OR ($3 < 0 AND r.chapter_index <= $4))
 UNION ALL
 SELECT f.id::text,r.id::text,'EVENT',f.source_chapter,f.valid_from_chapter,f.temporal,f.polarity,f.attribution,f.condition,f.source_value,
        NULL,NULL,f.action,f.arguments,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('field',COALESCE(arg->>'role','argument'),'surface',COALESCE(ep.source_name,ref.surface,arg->>'entity_id'),'entity_id',ep.persistent_entity_id,'reference_id',CASE WHEN ep.persistent_entity_id IS NULL THEN ref.reference_id END) ORDER BY a.ord)
                    FROM jsonb_array_elements(f.arguments) WITH ORDINALITY a(arg,ord)
                    LEFT JOIN fact_first_entity_proposal ep ON ep.run_id=f.run_id AND ep.proposal_id=arg->>'entity_id'
                    LEFT JOIN fact_first_reference ref ON ref.run_id=f.run_id AND ref.reference_id=arg->>'entity_id'
                   WHERE arg ? 'entity_id'),'[]'::jsonb),
        jsonb_build_array(jsonb_build_object('field','action','source',f.action,'rendered',rr.target_value,'render_status',COALESCE(rr.status,'pending'))), f.evidence,
        row_number() OVER (ORDER BY f.source_chapter,f.local_id,f.id)
   FROM fact_first_event f JOIN fact_first_run r ON r.id=f.run_id
   LEFT JOIN fact_first_rendering rr ON rr.run_id=f.run_id AND rr.assertion_id=f.assertion_id AND rr.output_kind='native_assertion' AND rr.output_id=f.local_id
  WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.status='published' AND f.source_chapter<=$4
    AND (($3 >= 0 AND r.chapter_index=$3) OR ($3 < 0 AND r.chapter_index <= $4))
)
SELECT jsonb_build_object('id',id,'run_id',run_id,'type',type,'original_index',original_index,'source_chapter',source_chapter,
 'valid_from_chapter',valid_from_chapter,'temporal_qualifier',temporal_qualifier,'polarity',polarity,'attribution',attribution,
 'condition',condition,'source_value',source_value,'action',action,'arguments',arguments,'relation',label,'values',value_json,
 'participants',participants,'evidence',COALESCE((SELECT jsonb_agg(jsonb_build_object('passage_id',p->>'id','run_id',run_id,'text',p->>'text','char_start',(p->>'char_start')::int,'char_end',(p->>'char_end')::int,'ordinal',ord-1) ORDER BY ord) FROM jsonb_array_elements(evidence) WITH ORDINALITY ep(p,ord)),'[]'::jsonb))
 FROM native WHERE ($6='' OR EXISTS (SELECT 1 FROM jsonb_array_elements(participants) p WHERE p->>'entity_id'=$6))
 ORDER BY source_chapter DESC, original_index DESC LIMIT $5`
	rows, err := tx.Query(ctx, query, novelID, generation, chapter, at, 500, entityID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RecordView
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var row RecordView
		if err := json.Unmarshal(raw, &row); err != nil {
			return nil, err
		}
		for i := range row.Evidence {
			row.Evidence[i].RunID = row.RunID
			row.Evidence[i].Chapter = row.SourceChapter
		}
		out = append(out, row)
	}
	return out, rows.Err()
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
		if err != nil {
			return err
		}
		native, err := factFirstRows(ctx, tx, novelID, generation, -1, at, "")
		if err != nil {
			return err
		}
		for _, row := range native {
			if row.Type == "EVENT" {
				out.Rows = append(out.Rows, row)
			}
		}
		return nil
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
		if err != nil {
			return err
		}
		native, err := factFirstRows(ctx, tx, novelID, generation, -1, at, "")
		if err != nil {
			return err
		}
		for _, row := range native {
			if row.Type == "FACT" || row.Type == "RELATION" {
				out.Rows = append(out.Rows, row)
			}
		}
		return nil
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
		// Fact-first proposals are bound to persistent entities only after chronological
		// identity resolution. Include bound native rows in hover cards; unresolved
		// references intentionally remain unbound and are shown only in chapter audits.
		native, nativeErr := factFirstRows(ctx, tx, novelID, generation, -1, at, entityID)
		if nativeErr != nil {
			return nativeErr
		}
		for _, row := range native {
			for _, participant := range row.Participants {
				if participant.EntityID != nil && *participant.EntityID == entityID {
					out.Entity.Records = append(out.Entity.Records, row)
					break
				}
			}
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
		if status.Counts != nil {
			out.Counts = status.Counts
		}
		out.SelectionOutcome = status.SelectionOutcome
		out.Stages = status.Stages
		if generation == "" {
			return nil
		}
		if err := tx.QueryRow(ctx, `SELECT
			(SELECT count(*) FROM record_row w WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter=$3)
			 + (SELECT count(*) FROM (SELECT run_id,assertion_id FROM fact_first_fact UNION ALL SELECT run_id,assertion_id FROM fact_first_relation UNION ALL SELECT run_id,assertion_id FROM fact_first_event) p JOIN fact_first_run r ON r.id=p.run_id WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.chapter_index=$3),
			(SELECT count(*) FROM record_drop d JOIN record_run r ON r.id=d.run_id WHERE d.novel_id=$1 AND d.generation_id=$2 AND r.chapter_index=$3)
			 + (SELECT count(DISTINCT (r.id, x.value->'row'->>'assertion_id')) FILTER (WHERE x.value->'row'->>'assertion_id' IS NOT NULL) FROM fact_first_run r CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.diagnostics->'normalization'->'rejected','[]'::jsonb)) x(value) WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.chapter_index=$3),
			(SELECT count(*) FROM record_reference f JOIN record_run r ON r.id=f.run_id WHERE f.novel_id=$1 AND f.generation_id=$2 AND r.chapter_index=$3)
			 + (SELECT count(*) FROM fact_first_reference f JOIN fact_first_run r ON r.id=f.run_id WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.chapter_index=$3 AND f.persistent_entity_id IS NULL),
			(SELECT count(*) FROM record_rendering g JOIN record_row w ON w.id=g.row_id WHERE w.novel_id=$1 AND w.generation_id=$2 AND w.source_chapter=$3 AND g.status='failed')
			 + (SELECT count(*) FROM fact_first_rendering g JOIN fact_first_run r ON r.id=g.run_id WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.chapter_index=$3 AND g.output_kind='native_assertion' AND g.status='failed')`,
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
		if err := drops.Err(); err != nil {
			return err
		}
		// Native normalization keeps rejected rows in diagnostics rather than record_drop.
		// Surface those reasons in the same inspector list, keyed by the row index when
		// available; assertion_id is nested in the persisted rejected-row shape.
		nativeDrops, err := tx.Query(ctx, `SELECT COALESCE((x.value->>'index')::int,-1),
			ARRAY[COALESCE(x.value->>'reason','normalization rejected')]
			FROM fact_first_run r
			CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.diagnostics->'normalization'->'rejected','[]'::jsonb)) x(value)
			WHERE r.novel_id=$1 AND r.generation_id=$2::uuid AND r.chapter_index=$3
			  AND x.value->'row'->>'assertion_id' IS NOT NULL
			ORDER BY COALESCE((x.value->>'index')::int,-1) LIMIT 200`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		for nativeDrops.Next() {
			var view RecordDropView
			if err := nativeDrops.Scan(&view.OriginalIndex, &view.Reasons); err != nil {
				nativeDrops.Close()
				return err
			}
			out.Drops = append(out.Drops, view)
		}
		nativeDrops.Close()
		return nativeDrops.Err()
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
		// The reader gets a generic message, but something has to record the cause. Without
		// this, a permission-denied on one column was indistinguishable from any other
		// failure: the surface said "could not load records" and the logs said nothing.
		log.Printf("load records: %v", err)
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
	case chapter == "" && strings.HasSuffix(r.URL.Path, "/extract"):
		action = "extract"
	case chapter == "":
		action = "rebuild"
	case strings.HasSuffix(r.URL.Path, "/render-retry"):
		action = "render-retry"
	case strings.HasSuffix(r.URL.Path, "/discard"):
		action = "discard"
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
