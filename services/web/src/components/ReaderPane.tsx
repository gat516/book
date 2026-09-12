import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getChapter, getRecords, putProgress } from "../api";
import type { ChapterResponse, EntityView } from "../types";
import { HoverCard } from "./HoverCard";
import { EntityInspector } from "./EntityInspector";
import { usePolling } from "../usePolling";
import { applyRenderingChoices, lastMentionPerEntity, segment } from "../readerSegments";
import { RecordList } from "./RecordList";
import { TermList } from "./TermList";
import type { RecordsResponse } from "../types";
import { uniqueChapterRenderings } from "../recordPresentation";

interface Props {
  novelId: string;
  chapterIndex: number;
  clickableEntities: boolean;
  onChapterLoaded: (chapter: ChapterResponse) => void;
  // Called instead of rendering an error when the requested chapter (and typically every
  // chapter — a brand-new novel) doesn't exist yet, so the caller can offer to add one
  // instead of showing a raw "chapter is missing or not done" string.
  onNoChapter: () => void;
  // Opens the Knowledge repair panel. Optional so the reader still renders standalone in
  // contexts (tests, the pending view) that have no panel to open.
  onOpenRepair?: () => void;
}

// Record extraction can take minutes on a local model. Pending means the worker has not
// published a run yet, so check slowly; once processing starts, tighter checks make the
// transition to linked spans feel live without hammering the API.
const RECORD_PENDING_POLL_MS = 20000;
const RECORD_PROCESSING_POLL_MS = 8000;

export function ReaderPane({ novelId, chapterIndex, clickableEntities, onChapterLoaded, onNoChapter }: Props) {
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);
  const [selected, setSelected] = useState<{ id: string | null; mention: string } | null>(null);
  const [records, setRecords] = useState<RecordsResponse | null>(null);
  const [recordsError, setRecordsError] = useState<string | null>(null);

  // Both hover and click views share only the exact novel/chapter/clearance cache.
  // The server's `at` becomes known on load; changing it discards earlier entity data.
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, chapterIndex, chapter?.at, records?.status.generation_id, records?.status.version]);

  useEffect(() => {
    setSelected(null);
    setHovered(null);
    setRecords(null);
    setRecordsError(null);
  }, [novelId, chapterIndex, clickableEntities]);

  useEffect(() => {
    let cancelled = false;
    setChapter(null);
    setError(null);

    async function load(): Promise<ChapterResponse | "no-chapter"> {
      try {
        return await getChapter(novelId, chapterIndex);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404 && err.code === "chapter not found") {
          return "no-chapter";
        }
        // A brand-new reader has no `reader_progress` row yet, so every gated endpoint
        // 404s — including this one — until one exists (reader-api's design, not a bug).
        // A first-time reader just wants to start reading, so bootstrap progress to this
        // chapter and retry once, rather than surfacing a raw 404 as the landing state.
        if (err instanceof ApiError && err.status === 404 && err.code === "reader progress not found") {
          try {
            await putProgress(novelId, chapterIndex);
          } catch (putErr) {
            // The novel has no chapters yet (or not this one) — AdvanceProgress only
            // succeeds against a chapter with status='done', so this 409 means "nothing
            // to read here" rather than a real failure.
            if (putErr instanceof ApiError && putErr.status === 409) {
              return "no-chapter";
            }
            throw putErr;
          }
          return await getChapter(novelId, chapterIndex);
        }
        throw err;
      }
    }

    load()
      .then((response) => {
        if (cancelled) return;
        if (response === "no-chapter") {
          onNoChapter();
          return;
        }
        setChapter(response);
        // The chapter response already carries the records introduced at this exact
        // source chapter. Use that payload for the first paint; the rows endpoint is
        // only needed while extraction/rendering is still in flight.
        setRecords(response.records_status ? {
          novel_id: response.novel_id,
          chapter_index: response.chapter_index,
          at: response.at,
          status: response.records_status,
          rows: response.record_rows ?? [],
        } : null);
        onChapterLoaded(response);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex]);

  const generation = useRef(0);
  const polling = useRef(false);
  useEffect(() => { generation.current++; }, [novelId, chapterIndex]);
  usePolling(() => {
    if (!chapter || polling.current) return;
    const current = generation.current;
    polling.current = true;
    getRecords(novelId, chapterIndex).then(async (recordStatus) => {
      if (current !== generation.current) return;
      const previous = records?.status;
      setRecords(recordStatus);
      setRecordsError(null);
      const extractionBecameReady = recordStatus.status.extraction_status === "ready" && previous?.extraction_status !== "ready";
      const renderingBecameReady = recordStatus.status.rendering_status === "ready" && previous?.rendering_status !== "ready";
      if (!extractionBecameReady && !renderingBecameReady) return;
      // Close old cards immediately; late responses cannot repopulate the new cache.
      cache.clear(); setSelected(null); setHovered(null);
      setChapter(previous => previous ? {...previous, spans: previous.spans.map(span => ({...span, entity_id: null}))} : previous);
      const refreshed = await getChapter(novelId, chapterIndex);
      if (current !== generation.current) return;
      setChapter(refreshed); onChapterLoaded(refreshed);
    }).catch((reason) => {
      if (current === generation.current) setRecordsError(errorMessage(reason));
    })
      .finally(() => { polling.current = false; });
  }, recordPollInterval(records), chapter !== null && recordsError === null && !recordsTerminal(records));

  if (error) return <p className="reader-pane-error">Could not load chapter: {error}</p>;
  if (!chapter) return <p>Loading chapter…</p>;

  // Render every confirmed occurrence with the preferred spelling, while keeping only one
  // interactive anchor per distinct thing. The stored translation remains unchanged.
  const rendered = applyRenderingChoices(chapter.text, chapter.spans);
  const segments = segment(rendered.text, lastMentionPerEntity(rendered.text, rendered.spans));
  return (
    <div className="reader-pane">
      <p className="reader-pane-chapter-label">
        Chapter {chapterIndex}
        {chapter.site_chapter_no && (
          <span className="reader-pane-site-chapter-no">
            {" "}
            — {chapter.site_chapter_no}
            {/* This site paginates a chapter across several pages, each ingested as its
                own chapter row; without the part number a reader can't tell why the text
                stops mid-scene. Hidden when the chapter isn't split. */}
            {chapter.part > 1 && ` (part ${chapter.part})`}
          </span>
        )}
        {chapter.source_url && (
          <>
            {" "}— <a href={chapter.source_url} target="_blank" rel="noreferrer">Open source chapter ↗</a>
          </>
        )}
      </p>
      {recordsError && <p role="alert" className="reader-records-error">
        Could not load chapter knowledge: {recordsError} <button type="button" onClick={() => {
          setRecordsError(null);
          void getRecords(novelId, chapterIndex).then(setRecords).catch((reason) => setRecordsError(errorMessage(reason)));
        }}>Retry</button>
      </p>}
      {records?.status.extraction_status === "processing" && <p role="status">Extracting chapter records…</p>}
      {records?.status.extraction_status === "failed" && <p role="status">Record extraction failed. The chapter is still readable{records.status.failure_detail ? ` (${records.status.failure_detail})` : ""}.</p>}
      {chapter.translation_warning?.code === "locked_terms_missing" && <p role="status" className="reader-translation-warning">
        This chapter is readable, but {chapter.translation_warning.term_count} locked name{chapter.translation_warning.term_count === 1 ? " was" : "s were"} not preserved exactly.
      </p>}
      {records && <RecordList rows={records.rows} status={records.status} title="Facts and records learned here" onEntity={(id, surface) => setSelected({id, mention: surface})} />}
      <TermList renderings={uniqueChapterRenderings(chapter.spans)} title="Terms used here" />
      {clickableEntities && chapter.spans.length === 0 && <p className="reader-entity-hint">
        No named mentions are available for this chapter yet. Cards do not require facts or a glossary entry.
      </p>}
      {segments.map((piece, index) => {
        if (!piece.mention) return <span key={index}>{piece.text}</span>;
        return (
          <span className="mention-anchor" key={index}
            onMouseEnter={() => { if (!clickableEntities && !selected) setHovered(index); }}
            onMouseLeave={() => setHovered((current) => current === index ? null : current)}>
            <button
              type="button"
              className={`mention mention-button${piece.entityId ? "" : " mention-unlinked"}${piece.rendering?.status === "locked" ? " mention-confirmed" : ""}`}
              aria-haspopup="dialog"
              aria-label={`Inspect ${piece.text}`}
              onClick={() => { setHovered(null); setSelected({ id: piece.entityId, mention: piece.text }); }}
            >{piece.text}</button>
            {hovered === index && (
              <HoverCard
                novelId={novelId}
                entityId={piece.entityId}
                rendering={piece.rendering}
                status={records?.status.extraction_status}
                mention={piece.text}
                at={chapter.at}
                cache={cache}
                onRenderingChanged={(updatedRendering) => setChapter((current) => current ? {
                  ...current,
                  spans: current.spans.map((span) => {
                    const surface = Array.from(current.text).slice(span.char_start, span.char_end).join("");
                    return span.rendering?.source_term === updatedRendering.source_term ||
                      (!span.rendering && surface === piece.text)
                      ? { ...span, rendering: updatedRendering }
                      : span;
                  }),
                } : current)}
                onClose={() => setHovered(null)}
              />
            )}
          </span>
        );
      })}
      {selected && <EntityInspector
        key={`${novelId}:${chapterIndex}:${chapter.at}:${selected.id}`}
        novelId={novelId}
        entityId={selected.id}
        status={records?.status.extraction_status}
        mention={selected.mention}
        at={chapter.at}
        cache={cache}
        onClose={() => setSelected(null)}
      />}
    </div>
  );
}

function recordsTerminal(records: RecordsResponse | null): boolean {
  if (!records) return false;
  if (records.status.extraction_status === "failed") return true;
  const extractionDone = records.status.extraction_status === "ready" || records.status.extraction_status === "failed";
  const renderingDone = records.status.rendering_status === "ready" || records.status.rendering_status === "failed";
  return extractionDone && renderingDone;
}

function recordPollInterval(records: RecordsResponse | null): number {
  if (!records) return RECORD_PENDING_POLL_MS;
  return records.status.extraction_status === "processing" ||
    (records.status.extraction_status === "ready" && records.status.rendering_status === "pending")
    ? RECORD_PROCESSING_POLL_MS
    : RECORD_PENDING_POLL_MS;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
