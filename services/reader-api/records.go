package main

import (
	"context"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"net/http"
	"strconv"
)

func (s *Store) ListRecords(ctx context.Context, novelID string, chapter, at int) (RecordsResponse, error) {
	out := RecordsResponse{NovelID: novelID, ChapterIndex: chapter, Rows: []RecordView{}}
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		if chapter > at {
			return ErrNotFound
		}
		var status string
		var extraction, rendering string
		var generation string
		var version int64
		var warnings int
		var detail *string
		err := tx.QueryRow(ctx, `SELECT g.id::text,
   COALESCE((SELECT status FROM record_run r WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index=$2),'pending'),
   COALESCE((SELECT CASE WHEN bool_or(rr.status='failed') THEN 'failed' WHEN bool_and(rr.status='ready') THEN 'ready' ELSE 'pending' END FROM record_rendering rr JOIN record_row r ON r.id=rr.row_id JOIN record_run x ON x.id=r.run_id WHERE x.novel_id=$1 AND x.generation_id=g.id AND x.chapter_index=$2),'pending'),
   COALESCE((SELECT max(publication_version) FROM record_run r WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index <= $2),0),
   COALESCE((SELECT warning_count FROM record_run r WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index=$2),0),
   (SELECT diagnostics->>'failure' FROM record_run r WHERE r.novel_id=$1 AND r.generation_id=g.id AND r.chapter_index=$2)
   FROM novel n JOIN record_generation g ON g.id=n.active_record_generation WHERE n.id=$1`, novelID, chapter).
			Scan(&generation, &extraction, &rendering, &version, &warnings, &detail)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		status = extraction
		if status == "published" {
			status = "ready"
		}
		out.Status = RecordsStatus{GenerationID: generation, Version: fmt.Sprintf("%s:%d", generation, version), ExtractionStatus: status, RenderingStatus: rendering, WarningCount: warnings, FailureDetail: detail}
		rows, err := tx.Query(ctx, `SELECT id::text,record_type,original_index,source_chapter,valid_from_chapter,temporal_qualifier FROM record_row WHERE novel_id=$1 AND generation_id=$2 AND source_chapter=$3 ORDER BY original_index LIMIT 500`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		for rows.Next() {
			var row RecordView
			if err := rows.Scan(&row.ID, &row.Type, &row.OriginalIndex, &row.SourceChapter, &row.ValidFromChapter, &row.TemporalQualifier); err != nil {
				return err
			}
			row.Values = []RecordValueView{}
			row.Participants = []RecordParticipantView{}
			row.Evidence = []RecordEvidenceView{}
			out.Rows = append(out.Rows, row)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		rows.Close()
		for i := range out.Rows {
			row := &out.Rows[i]
			vals, err := tx.Query(ctx, `SELECT v.field_name,v.source_value,COALESCE(r.target_value,''),COALESCE(r.status,'pending') FROM record_value v LEFT JOIN record_rendering r ON r.row_id=v.row_id AND r.field_name=v.field_name WHERE v.row_id=$1 ORDER BY v.field_name`, row.ID)
			if err != nil {
				return err
			}
			for vals.Next() {
				var v RecordValueView
				if err := vals.Scan(&v.Field, &v.Source, &v.Rendered, &v.RenderStatus); err != nil {
					vals.Close()
					return err
				}
				row.Values = append(row.Values, v)
			}
			vals.Close()
			ev, err := tx.Query(ctx, `SELECT e.passage_id,e.quote,p.text,p.char_start,p.char_end,p.ordinal FROM record_evidence e JOIN record_passage p ON p.run_id=e.run_id AND p.passage_id=e.passage_id WHERE e.row_id=$1 ORDER BY p.ordinal`, row.ID)
			if err != nil {
				return err
			}
			for ev.Next() {
				var e RecordEvidenceView
				if err := ev.Scan(&e.PassageID, &e.Quote, &e.Text, &e.CharStart, &e.CharEnd, &e.Ordinal); err != nil {
					ev.Close()
					return err
				}
				row.Evidence = append(row.Evidence, e)
			}
			ev.Close()
			parts, err := tx.Query(ctx, `SELECT field_name,surface,entity_id::text,reference_id::text FROM record_participant WHERE row_id=$1 ORDER BY ordinal`, row.ID)
			if err != nil {
				return err
			}
			for parts.Next() {
				var p RecordParticipantView
				if err := parts.Scan(&p.Field, &p.Surface, &p.EntityID, &p.ReferenceID); err != nil {
					parts.Close()
					return err
				}
				row.Participants = append(row.Participants, p)
			}
			parts.Close()
		}
		return nil
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
	if errors.Is(err, ErrNotFound) {
		writeError(w, http.StatusNotFound, "novel not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load records")
		return
	}
	writeJSON(w, http.StatusOK, out)
}
