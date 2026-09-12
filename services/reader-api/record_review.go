package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
)

// ListRecordReviews is a spoiler-gated operator projection. The SQL function is a
// narrowly scoped SECURITY DEFINER projection because record_row RLS correctly hides a
// rejected row from ordinary reads; the decision overlay must still show that row so a
// reviewer can accept it again. Unreviewed rows remain visible by default.
func (s *Store) ListRecordReviews(ctx context.Context, novelID string, chapter, at int) (RecordReviewResponse, error) {
	out := RecordReviewResponse{NovelID: novelID, ChapterIndex: chapter, At: at, Items: []RecordReviewItemView{}}
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
		rows, err := tx.Query(ctx, `SELECT row_id,record_type,original_index,source_chapter,
			valid_from_chapter,temporal_qualifier,values_json,participants_json,evidence_json
			FROM reader_record_review_rows($1,$2,$3)`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		defer rows.Close()
		byID := make(map[string]int)
		for rows.Next() {
			var row RecordView
			var values, participants, evidence []byte
			if err := rows.Scan(&row.ID, &row.Type, &row.OriginalIndex, &row.SourceChapter,
				&row.ValidFromChapter, &row.TemporalQualifier, &values, &participants, &evidence); err != nil {
				return err
			}
			if err := json.Unmarshal(values, &row.Values); err != nil {
				return err
			}
			if err := json.Unmarshal(participants, &row.Participants); err != nil {
				return err
			}
			if err := json.Unmarshal(evidence, &row.Evidence); err != nil {
				return err
			}
			byID[row.ID] = len(out.Items)
			out.Items = append(out.Items, RecordReviewItemView{Row: row})
		}
		if err := rows.Err(); err != nil {
			return err
		}
		decisions, err := tx.Query(ctx, `SELECT DISTINCT ON (row_id) row_id::text,decision,actor,reason,request_id,created_at
			FROM record_review_decision
			WHERE novel_id=$1 AND generation_id=$2 AND source_chapter=$3
			ORDER BY row_id,created_at DESC,id DESC`, novelID, generation, chapter)
		if err != nil {
			return err
		}
		defer decisions.Close()
		for decisions.Next() {
			var rowID string
			var decision RecordReviewDecisionView
			if err := decisions.Scan(&rowID, &decision.Decision, &decision.Actor, &decision.Reason,
				&decision.RequestID, &decision.CreatedAt); err != nil {
				return err
			}
			if index, ok := byID[rowID]; ok {
				out.Items[index].Decision = &decision
			}
		}
		return decisions.Err()
	})
	return out, err
}

func (a *API) getRecordReviews(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	out, err := a.store.ListRecordReviews(r.Context(), novel, chapter, at)
	a.writeRecords(w, out, err)
}

func (a *API) patchRecordReview(w http.ResponseWriter, r *http.Request) {
	reader, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 || chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, paramsRequestLimit))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	var value map[string]any
	if err := json.Unmarshal(body, &value); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	// The browser cannot impersonate another reviewer. Ingest records this trusted
	// identity in the append-only audit row; any client-supplied actor is overwritten.
	value["actor"] = reader
	body, _ = json.Marshal(value)
	result, status, err := a.ingest.ReviewRecord(r.Context(), novel, strconv.Itoa(chapter), body)
	if err != nil {
		log.Printf("record review: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}

func (a *API) discardRecordRebuild(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novel, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, paramsRequestLimit))
	if err != nil {
		writeError(w, http.StatusBadRequest, "could not read request body")
		return
	}
	result, status, err := a.ingest.DiscardRecordsRebuild(r.Context(), novel, body)
	if err != nil {
		log.Printf("discard records rebuild: %v", err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
