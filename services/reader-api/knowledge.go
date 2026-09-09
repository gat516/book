package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

type KnowledgeStatus struct {
	RevisionID string `json:"revision_id"`
	Version    int64  `json:"version"`
	Trusted    bool   `json:"trusted"`
	Status     string `json:"status"`
	// Legacy is true for a revision nobody has ever rebuilt (0023's initialize trigger
	// creates every novel's first revision this way: active, trusted AND legacy). A
	// legacy revision is trusted but cannot take a per-chapter extraction — KnowledgeEngine
	// requires a pinned model a legacy revision never recorded — so Trusted alone is not
	// "writable" (migration 0058).
	Legacy bool `json:"legacy"`
	// ChapterSnapshotted reports whether this chapter is one prepare() actually read when
	// it built the active managed revision's snapshot. A chapter added afterward has
	// nothing to append its facts to until the graph is rebuilt again.
	ChapterSnapshotted bool `json:"chapter_snapshotted"`
	// CanExtract is the real predicate ingest-api enforces before running a per-chapter
	// extraction: trusted, managed (not legacy) and snapshotted. Derived in SQL
	// (reader_knowledge_status) so Go reports it rather than reimplementing a gate.
	CanExtract bool `json:"can_extract"`
}

// blockedReason names the one cause chapter-scoped extraction is unavailable, so the
// client can render an accurate sentence instead of "read-only because not trusted" for
// every book that has never been rebuilt. Presentation only — CanExtract already carries
// the policy; this only picks among the mutually exclusive reasons it can be false.
// Order matters: an unfinished rebuild (not trusted) is reported before "never built"
// even for a book whose only revision is both legacy and quarantined, because a rebuild
// in progress needs Review/Activate/Discard, not "Build chapter knowledge" again.
func blockedReason(k KnowledgeStatus) string {
	switch {
	case k.CanExtract:
		return ""
	case k.Status == "unavailable":
		return "never_built"
	case !k.Trusted:
		return "quarantined"
	case k.Legacy:
		return "never_built"
	default:
		// The three cases above exhaust the ways CanExtract can be false today
		// (trusted AND NOT legacy AND snapshotted), so this is the missing-chapter
		// one. It stays a non-empty reason rather than a bare "" default: an empty
		// string means "not blocked", and reporting that while CanExtract is false
		// would hand the client a writable-looking gate it must not render.
		return "chapter_not_snapshotted"
	}
}

func (s *Store) ChapterKnowledge(ctx context.Context, novel string, chapter, at int) (ChapterKnowledgeView, error) {
	view := ChapterKnowledgeView{NovelID: novel, ChapterIndex: chapter, Facts: []ChapterFactView{}, Terms: []ChapterTermView{}}
	err := s.withReaderTx(ctx, novel, at, func(tx pgx.Tx) error {
		status, err := knowledgeInTx(ctx, tx, chapter)
		if err != nil {
			return err
		}
		view.RevisionID = status.RevisionID
		view.Version = status.Version
		view.Trusted = status.Trusted
		view.Status = status.Status
		view.Legacy = status.Legacy
		view.ChapterSnapshotted = status.ChapterSnapshotted
		view.CanExtract = status.CanExtract
		view.BlockedReason = blockedReason(status)
		rows, err := tx.Query(ctx, `SELECT f.id,f.entity_id::text,COALESCE(e.canonical_en,e.canonical),f.attribute,
			COALESCE(f.value_en,f.value),f.value,f.value_en,f.kind,f.supersedes,
			CASE WHEN EXISTS(SELECT 1 FROM fact s WHERE s.revision_id=f.revision_id AND s.supersedes=f.id)
			     THEN CASE WHEN EXISTS(SELECT 1 FROM fact s WHERE s.revision_id=f.revision_id AND s.supersedes=f.id AND s.kind='retraction') THEN 'retracted' ELSE 'superseded' END
			     ELSE 'active' END,
			COALESCE(jsonb_build_object('id',v.id,'chapter',v.chapter_index,'quote',v.quote,
			 'source_hash',v.source_hash,'char_start',v.char_start,'char_end',v.char_end),'null'::jsonb),
			f.valid_from_chapter,f.source_chapter,f.confidence
			 FROM fact f JOIN entity e ON e.id=f.entity_id LEFT JOIN graph_evidence v ON v.id=f.evidence_id
			 WHERE f.novel_id=$1 AND f.source_chapter=$2 AND f.kind<>'retraction' ORDER BY f.id`, novel, chapter)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var f ChapterFactView
			if err = rows.Scan(&f.ID, &f.EntityID, &f.EntityCanonical, &f.Attribute, &f.Value,
				&f.ValueSource, &f.ValueEN, &f.Kind, &f.Supersedes, &f.Status, &f.Evidence, &f.ValidFromChapter, &f.SourceChapter, &f.Confidence); err != nil {
				return err
			}
			view.Facts = append(view.Facts, f)
		}
		if err = rows.Err(); err != nil {
			return err
		}
		terms, err := tx.Query(ctx, `SELECT t.source_term,COALESCE(g.target_term,t.display_term),t.char_start,t.char_end,
			COALESCE(g.locked_at_chapter=$2,false),COALESCE(g.deleted,false)
		 FROM term_rendering_occurrence t LEFT JOIN glossary g
		   ON g.novel_id=t.novel_id AND g.source_term=t.source_term AND g.locked_at_chapter<=reader_chapter()
		 WHERE t.novel_id=$1 AND t.chapter_index=$2 ORDER BY t.char_start`, novel, chapter)
		if err != nil {
			return err
		}
		defer terms.Close()
		for terms.Next() {
			var term ChapterTermView
			if err = terms.Scan(&term.SourceTerm, &term.TargetTerm, &term.CharStart, &term.CharEnd, &term.NewInChapter, &term.Deleted); err != nil {
				return err
			}
			view.Terms = append(view.Terms, term)
		}
		if err = terms.Err(); err != nil {
			return err
		}
		// A successful extraction remains meaningful even when it found zero rows. Derive
		// completion from the scoped run ledger, not from Facts/Terms length (§0.2).
		var termsRunPublished, factsRunPublished bool
		err = tx.QueryRow(ctx, `SELECT
			COALESCE(bool_or(state='published' AND scope IN ('all','terms')),false),
			COALESCE(bool_or(state='published' AND scope IN ('all','facts')),false)
		 FROM chapter_knowledge_run WHERE novel_id=$1 AND chapter_index=$2`, novel, chapter).
			Scan(&termsRunPublished, &factsRunPublished)
		if err != nil {
			return err
		}
		// Translation's display scan also extracts real, reviewable term occurrences. If
		// they are visible above, reporting "Not extracted" is self-contradictory. The run
		// bit still records a successful zero-result explicit extraction.
		view.TermsExtracted = len(view.Terms) > 0 || termsRunPublished
		view.FactsExtracted = factsRunPublished
		// A full-book rebuild writes into an untrusted staging revision. Do not expose its
		// claims here: the active-revision RLS fence is what keeps cards and Ask AI free of
		// unreviewed knowledge (§0). Aggregate diagnostics are safe and prevent a completed
		// rebuild from being presented as "Not extracted" merely because it is not active.
		var graphExtraction ChapterGraphExtractionView
		err = tx.QueryRow(ctx, `SELECT state,verified_terms,verified_claims,published_fact_rows
		 FROM reader_chapter_graph_extraction($1,$2)`, novel, chapter).Scan(
			&graphExtraction.State, &graphExtraction.VerifiedTerms,
			&graphExtraction.VerifiedClaims, &graphExtraction.PublishedFactRows)
		if err == nil {
			view.GraphExtraction = &graphExtraction
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		var run ChapterKnowledgeRunView
		var preview []byte
		var blockedCategory *string
		var blockedAt *time.Time
		err = tx.QueryRow(ctx, `SELECT id::text,mode,scope,state,created_at,preview,blocked_category,blocked_at FROM chapter_knowledge_run
		 WHERE novel_id=$1 AND chapter_index=$2 ORDER BY created_at DESC LIMIT 1`, novel, chapter).
			Scan(&run.ID, &run.Mode, &run.Scope, &run.State, &run.CreatedAt, &preview, &blockedCategory, &blockedAt)
		if err == nil {
			if len(preview) > 0 {
				run.Preview = json.RawMessage(preview)
			}
			run.BlockedAt = blockedAt
			if blockedCategory != nil && *blockedCategory != "" {
				run.BlockedCategory = *blockedCategory
				run.BlockedDetail = chapterKnowledgeBlockedDetail(*blockedCategory)
			}
			view.Run = &run
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		return nil
	})
	return view, err
}

// chapterKnowledgeBlockedDetail is presentation-only. The pipeline stores the safe
// category beside its private error diagnostics; reader-api renders the same fixed
// sentence used by repair status and never returns chapter_knowledge_run.error.
func chapterKnowledgeBlockedDetail(category string) string {
	if detail := repairFailureDetail[category]; detail != "" {
		return detail
	}
	return repairFailureDetail[repairUnknownCause]
}

func (s *Store) ChapterKnowledgeActivity(ctx context.Context, novel string, chapter, at int, runID string, after int64) ([]ChapterKnowledgeActivity, error) {
	items := []ChapterKnowledgeActivity{}
	err := s.withReaderTx(ctx, novel, at, func(tx pgx.Tx) error {
		if runID == "" {
			_ = tx.QueryRow(ctx, `SELECT id::text FROM chapter_knowledge_run WHERE novel_id=$1 AND chapter_index=$2 ORDER BY created_at DESC LIMIT 1`, novel, chapter).Scan(&runID)
		}
		if runID == "" {
			return nil
		}
		rows, err := tx.Query(ctx, `SELECT seq,run_id::text,item_kind,item_key,phase,payload,created_at
		 FROM chapter_knowledge_activity WHERE novel_id=$1 AND chapter_index=$2 AND run_id=$3 AND seq>$4 ORDER BY seq LIMIT 200`, novel, chapter, runID, after)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var item ChapterKnowledgeActivity
			if err = rows.Scan(&item.Sequence, &item.RunID, &item.ItemKind, &item.ItemKey, &item.Phase, &item.Payload, &item.CreatedAt); err != nil {
				return err
			}
			items = append(items, item)
		}
		return rows.Err()
	})
	return items, err
}

func (a *API) getChapterKnowledge(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	view, err := a.store.ChapterKnowledge(r.Context(), novel, chapter, at)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load chapter knowledge")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, view)
}

func (a *API) getChapterKnowledgeActivity(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.PathValue("n"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	after := int64(0)
	if raw := r.URL.Query().Get("after"); raw != "" {
		after, err = strconv.ParseInt(raw, 10, 64)
		if err != nil || after < 0 {
			writeError(w, http.StatusBadRequest, "invalid activity sequence")
			return
		}
	}
	items, err := a.store.ChapterKnowledgeActivity(r.Context(), novel, chapter, at, r.URL.Query().Get("run_id"), after)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load chapter activity")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"activity": items})
}

func knowledgeInTx(ctx context.Context, tx pgx.Tx, chapter int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := tx.QueryRow(ctx, `SELECT revision_id::text,version,trusted,status,legacy,chapter_snapshotted,can_extract
		FROM reader_knowledge_status($1)`, chapter).
		Scan(&k.RevisionID, &k.Version, &k.Trusted, &k.Status, &k.Legacy, &k.ChapterSnapshotted, &k.CanExtract)
	// A reader may deliberately delete every graph revision while retaining the novel
	// and its chapters. That is an empty knowledge state, not a missing chapter. Keep
	// the read path available while graph-backed RLS continues to fail closed (§0).
	if errors.Is(err, pgx.ErrNoRows) {
		return KnowledgeStatus{Status: "unavailable"}, nil
	}
	return k, err
}

func (s *Store) KnowledgeStatus(ctx context.Context, novel string, chapter, at int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := s.withReaderTx(ctx, novel, at, func(tx pgx.Tx) error {
		var err error
		k, err = knowledgeInTx(ctx, tx, chapter)
		return err
	})
	return k, err
}

func eventKnowledgeInTx(ctx context.Context, tx pgx.Tx, chapter int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := tx.QueryRow(ctx, `SELECT revision_id::text,version,trusted,status FROM reader_event_status($1)`, chapter).
		Scan(&k.RevisionID, &k.Version, &k.Trusted, &k.Status)
	if errors.Is(err, pgx.ErrNoRows) {
		return KnowledgeStatus{Status: "unavailable"}, nil
	}
	return k, err
}

func (s *Store) EventStatus(ctx context.Context, novel string, chapter, at int) (KnowledgeStatus, error) {
	var k KnowledgeStatus
	err := s.withReaderTx(ctx, novel, at, func(tx pgx.Tx) error {
		var err error
		k, err = eventKnowledgeInTx(ctx, tx, chapter)
		return err
	})
	return k, err
}

func (a *API) getKnowledgeStatus(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.URL.Query().Get("chapter"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	k, err := a.store.KnowledgeStatus(r.Context(), novel, chapter, at)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load knowledge status")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, k)
}

func (a *API) getEventStatus(w http.ResponseWriter, r *http.Request) {
	_, novel, at, ok := a.gateAt(w, r, nil)
	if !ok {
		return
	}
	chapter, err := strconv.Atoi(r.URL.Query().Get("chapter"))
	if err != nil || chapter < 0 {
		writeError(w, http.StatusBadRequest, "invalid chapter")
		return
	}
	if chapter > at {
		writeError(w, http.StatusNotFound, "chapter not found")
		return
	}
	k, err := a.store.EventStatus(r.Context(), novel, chapter, at)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not load event status")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, k)
}

// Generation changes during a multi-query graph request invalidate the entire response.
func (a *API) knowledgeUnchanged(w http.ResponseWriter, r *http.Request, novel string, at int, before KnowledgeStatus) bool {
	after, err := a.store.KnowledgeStatus(r.Context(), novel, at, at)
	if err != nil || after.RevisionID != before.RevisionID || after.Version != before.Version {
		writeError(w, http.StatusConflict, "knowledge changed; retry request")
		return false
	}
	return true
}
