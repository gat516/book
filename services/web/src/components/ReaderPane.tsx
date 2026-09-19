import { useKnowledgeRevision } from "../knowledgeUpdates";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getChapter, getRecords, putProgress } from "../api";
import type { ChapterResponse, EntityView, TermRenderingView } from "../types";
import { HoverCard } from "./HoverCard";
import { EntityInspector } from "./EntityInspector";
import { usePolling } from "../usePolling";
import { applyRenderingChoices, segment } from "../readerSegments";
import type { RecordsResponse } from "../types";
import { uniqueChapterRenderings } from "../recordPresentation";
import { recordPollInterval, recordsTerminal } from "../recordPolling";
import { ChapterStatus } from "./ChapterStatus";
import { ChapterNames } from "./ChapterNames";

interface Props {
  novelId: string;
  chapterIndex: number;
  clickableEntities: boolean;
  onChapterLoaded: (chapter: ChapterResponse) => void;
  // Called instead of rendering an error when the requested chapter (and typically every
  // chapter — a brand-new novel) doesn't exist yet, so the caller can offer to add one
  // instead of showing a raw "chapter is missing or not done" string.
  onNoChapter: () => void;
}

export function ReaderPane({ novelId, chapterIndex, clickableEntities, onChapterLoaded, onNoChapter }: Props) {
  const knowledgeRevision = useKnowledgeRevision(novelId);
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);
  const [selected, setSelected] = useState<{ id: string | null; mention: string } | null>(null);
  const [records, setRecords] = useState<RecordsResponse | null>(null);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  const [showNames, setShowNames] = useState(false);

  // Both hover and click views share only the exact novel/chapter/clearance cache.
  // The server's `at` becomes known on load; changing it discards earlier entity data.
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, chapterIndex, chapter?.at, records?.status.generation_id, records?.status.version]);

  useEffect(() => {
    setSelected(null);
    setHovered(null);
    setRecords(null);
    setRecordsError(null);
    setShowNames(false);
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

  useEffect(() => {
    if (knowledgeRevision === 0) return;
    let cancelled = false;
    cache.clear(); setSelected(null); setHovered(null); setRecords(null);
    setChapter(previous => previous ? { ...previous, spans: previous.spans.map(span => ({ ...span, entity_id: null })) } : previous);
    Promise.all([getChapter(novelId, chapterIndex), getRecords(novelId, chapterIndex)])
      .then(([nextChapter, nextRecords]) => {
        if (cancelled) return;
        setChapter(nextChapter); setRecords(nextRecords); setRecordsError(null);
        onChapterLoaded(nextChapter);
      }).catch(reason => { if (!cancelled) setRecordsError(errorMessage(reason)); });
    return () => { cancelled = true; };
    // The revision invalidates knowledge; cache changes must not reload prose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex, knowledgeRevision]);

  const generation = useRef(0);
  const polling = useRef(false);
  useEffect(() => { generation.current++; }, [novelId, chapterIndex, knowledgeRevision]);
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
      const recordsVersionChanged = !!previous && (
        recordStatus.status.generation_id !== previous.generation_id ||
        recordStatus.status.version !== previous.version
      );
      if (!extractionBecameReady && !renderingBecameReady && !recordsVersionChanged) return;
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

  // Every occurrence of a name is interactive: highlighted while it awaits review, plain
  // but still hoverable once confirmed. The stored translation remains unchanged.
  const rendered = applyRenderingChoices(chapter.text, chapter.spans);
  const segments = segment(rendered.text, rendered.spans);
  const renderings = uniqueChapterRenderings(chapter.spans);
  const toReview = new Set(renderings.filter((item) => item.status !== "locked").map((item) => item.source_term)).size;
  // A spelling saved from the names list or a hover card applies to every span of it.
  const applyRendering = (updated: TermRenderingView, surface?: string) => setChapter((current) => current ? {
    ...current,
    spans: current.spans.map((span) => {
      const text = Array.from(current.text).slice(span.char_start, span.char_end).join("");
      return span.rendering?.source_term === updated.source_term || (!span.rendering && surface !== undefined && text === surface)
        ? { ...span, rendering: updated }
        : span;
    }),
  } : current);
  return (
    <div className="reader-pane">
      <header className="chapter-head">
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
        <div className="chapter-head-tools">
          <ChapterStatus novelId={novelId} chapter={chapterIndex} status={records?.status ?? null} />
          <button type="button" className="chapter-names-toggle" aria-expanded={showNames} onClick={() => setShowNames((open) => !open)}>
            Names{toReview > 0 ? <span className="chapter-names-count">{toReview} to review</span> : null}
          </button>
        </div>
      </header>
      {recordsError && <p role="alert" className="reader-records-error">
        Could not load this chapter’s status: {recordsError} <button type="button" onClick={() => {
          setRecordsError(null);
          void getRecords(novelId, chapterIndex).then(setRecords).catch((reason) => setRecordsError(errorMessage(reason)));
        }}>Retry</button>
      </p>}
      {showNames && <ChapterNames novelId={novelId} at={chapter.at} renderings={renderings} onChanged={applyRendering} />}
      {chapter.translation_warning?.code === "locked_terms_missing" && <p role="status" className="reader-translation-warning">
        {chapter.translation_warning.term_count} confirmed name{chapter.translation_warning.term_count === 1 ? " is" : "s are"} not spelled exactly as confirmed in this chapter’s text.
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
                onEntity={(id, surface) => { setHovered(null); setSelected({ id, mention: surface }); }}
                onRenderingChanged={(updatedRendering) => applyRendering(updatedRendering, piece.text)}
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

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
