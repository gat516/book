package main

// Reader-side FACTS status and controls. The status query runs inside withReaderTx, so
// the chapter GUCs the RLS policies read are set (§13.1); the explicit
// `chapter_index <= at` predicate is the second, independent layer. Facts text never
// crosses this path -- only a count and bounded failure categories (§0).

import (
	"context"
	"errors"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

// factsMaxAttempts mirrors the worker's MAX_ENRICHMENT_ATTEMPTS/MAX_PROVIDER_RETRY_ATTEMPTS.
const factsMaxAttempts = 5

// chapterFactsStatus reports one chapter's FACTS progress: pending, processing (a retry
// is scheduled), failed (retries exhausted), or ready (its facts are written, 0110).
func chapterFactsStatus(ctx context.Context, tx pgx.Tx, novelID string, chapter, at int) (FactsStatus, error) {
	var state string
	var detail, retryCategory *string
	var retryAttempts, enrichmentAttempts int
	var retryAt *time.Time
	var discarded bool
	var factsCount *int
	err := tx.QueryRow(ctx, `
SELECT CASE WHEN (c.enrichment_retry_at IS NOT NULL AND c.enrichment_attempts < $4)
             OR (c.provider_retry_at IS NOT NULL AND c.provider_retry_attempts < $4) THEN 'processing'
            WHEN EXISTS (SELECT 1 FROM chapter_failure cf
                          WHERE cf.novel_id=c.novel_id AND cf.chapter_index=c.chapter_index
                            AND ((c.provider_retry_attempts >= $4 AND c.provider_retry_at IS NULL
                                  AND cf.error_code='provider_retry_exhausted')
                              OR (c.enrichment_attempts >= $4 AND c.enrichment_retry_at IS NULL
                                  AND cf.error_code <> 'provider_retry_exhausted'))) THEN 'failed'
            WHEN c.translation_ready AND c.facts_count IS NOT NULL THEN 'ready'
            ELSE 'pending' END,
       (SELECT cf.error_code FROM chapter_failure cf
         WHERE cf.novel_id=c.novel_id AND cf.chapter_index=c.chapter_index
           AND ((c.provider_retry_attempts >= $4 AND c.provider_retry_at IS NULL
                 AND cf.error_code='provider_retry_exhausted')
             OR (c.enrichment_attempts >= $4 AND c.enrichment_retry_at IS NULL
                 AND cf.error_code <> 'provider_retry_exhausted'))
         ORDER BY cf.occurred_at DESC, cf.id DESC LIMIT 1),
       GREATEST(c.provider_retry_attempts, c.enrichment_attempts),
       LEAST(c.provider_retry_at, c.enrichment_retry_at),
       c.provider_retry_category, c.enrichment_attempts, c.enrichment_discarded, c.facts_count
  FROM chapter c
 WHERE c.novel_id=$1 AND c.chapter_index=$2 AND c.chapter_index <= $3`,
		novelID, chapter, at, factsMaxAttempts).
		Scan(&state, &detail, &retryAttempts, &retryAt, &retryCategory, &enrichmentAttempts,
			&discarded, &factsCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return FactsStatus{}, ErrNotFound
	}
	if err != nil {
		return FactsStatus{}, err
	}
	// A failure with a future durable retry is not terminal: the worker will retry it
	// without a user action. Provider exhaustion has no retry_at and remains failed.
	if state == "failed" && retryAt != nil && retryAt.After(time.Now()) &&
		(detail == nil || *detail != "provider_retry_exhausted") {
		state = "pending"
	}
	status := FactsStatus{
		State:            state,
		FailureDetail:    detail,
		RetryAttempts:    retryAttempts,
		RetryMaxAttempts: factsMaxAttempts,
		RetryAt:          retryAt,
		RetryCategory:    retryCategory,
		Discarded:        discarded,
		FactsCount:       factsCount,
	}
	// Generic stage retries have no provider_retry_category. Read their safe failure
	// ledger too (§0: only a bounded category crosses this path).
	if retryCategory == nil && (enrichmentAttempts > 0 || state == "failed") && !discarded {
		var code string
		err := tx.QueryRow(ctx, `SELECT error_code FROM chapter_failure
		 WHERE novel_id=$1 AND chapter_index=$2 AND chapter_index <= $3
		 ORDER BY occurred_at DESC, id DESC LIMIT 1`, novelID, chapter, at).Scan(&code)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return FactsStatus{}, err
		}
		if err == nil {
			code = safeChapterFailureCode(code)
			status.RetryCategory, status.FailureDetail = &code, &code
		}
	}
	return status, nil
}

func safeChapterFailureCode(code string) string {
	switch code {
	case "output_limit", "provider_content_filtered", "provider_invalid_json", "invalid_stage_output", "provider_bad_request",
		"credential_missing", "credential_rejected", "model_not_available", "model_not_installed",
		"prompt_too_large", "unsupported_schema", "output_truncated", "model_changed",
		"provider_timeout", "timeout", "provider_connection", "model_unreachable", "unreachable",
		"provider_http_400", "provider_http_401", "provider_http_403", "provider_http_404",
		"provider_http_413", "provider_http_422", "provider_http_429",
		"model_server_error", "rate_limited", "quota_exhausted", "provider_retry_exhausted",
		"provider_batch_failed", "stage_failed":
		return code
	default:
		return "stage_failed"
	}
}

func (s *Store) GetFactsStatus(ctx context.Context, novelID string, chapter, at int) (FactsStatus, error) {
	var out FactsStatus
	err := s.withReaderTx(ctx, novelID, at, func(tx pgx.Tx) error {
		var err error
		out, err = chapterFactsStatus(ctx, tx, novelID, chapter, at)
		return err
	})
	return out, err
}

func (a *API) getChapterFactsStatus(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gate(w, r)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	status, err := a.store.GetFactsStatus(r.Context(), novel, chapter, at)
	switch {
	case errors.Is(err, ErrNotFound):
		writeError(w, http.StatusNotFound, "not found")
	case err != nil:
		log.Printf("facts status: %v", err)
		writeError(w, http.StatusInternalServerError, "could not load facts status")
	default:
		writeJSON(w, http.StatusOK, ChapterFactsStatusResponse{
			NovelID: novel, ChapterIndex: chapter, At: at, Status: status,
		})
	}
}

// postFactsAction proxies the FACTS controls to ingest-api behind its internal token, the
// same path every other mutation takes. chapter is "" for the novel-wide actions.
func (a *API) postFactsAction(action string, perChapter bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		prepareReaderResponse(w)
		novelID, ok := pathUUID(r, "id")
		if !ok {
			writeError(w, http.StatusBadRequest, "invalid novel id")
			return
		}
		chapter := ""
		if perChapter {
			n, err := strconv.Atoi(r.PathValue("n"))
			if err != nil || n < 0 {
				writeError(w, http.StatusBadRequest, "invalid chapter")
				return
			}
			chapter = strconv.Itoa(n)
		}
		result, status, err := a.ingest.FactsAction(r.Context(), novelID, chapter, action)
		a.writeIngestResult(w, "facts "+action, result, status, err)
	}
}

// getFactsStatus is book-level operational metadata (counts, never facts), so it is
// ungated like the pipeline status; the ingest proxy still requires its internal token.
func (a *API) getFactsStatus(w http.ResponseWriter, r *http.Request) {
	prepareReaderResponse(w)
	novelID, ok := pathUUID(r, "id")
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid novel id")
		return
	}
	result, status, err := a.ingest.FactsStatus(r.Context(), novelID)
	a.writeIngestResult(w, "facts status", result, status, err)
}

func (a *API) writeIngestResult(w http.ResponseWriter, what string, result []byte, status int, err error) {
	if err != nil {
		log.Printf("%s: %v", what, err)
		writeError(w, http.StatusBadGateway, "ingest-api unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(result)
}
