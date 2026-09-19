import { useKnowledgeRevision } from "../knowledgeUpdates";
import { useEffect, useRef, useState } from "react";
import { ApiError, getChapter, getChapterFactsStatus, putProgress } from "../api";
import type { ChapterResponse, FactsStatus, TermRenderingView } from "../types";
import { HoverCard } from "./HoverCard";
import { usePolling } from "../usePolling";
import { applyRenderingChoices, segment } from "../readerSegments";
import { uniqueChapterRenderings } from "../recordPresentation";
import { factsPollInterval, factsTerminal } from "../factsPolling";
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
  const [facts, setFacts] = useState<FactsStatus | null>(null);
  const [factsError, setFactsError] = useState<string | null>(null);
  const [showNames, setShowNames] = useState(false);

  useEffect(() => {
    setHovered(null);
    setFacts(null);
    setFactsError(null);
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
        // The chapter response carries its facts status for the first paint; the status
        // endpoint is only polled while FACTS is still in flight.
        setFacts(response.facts_status ?? null);
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
    setHovered(null);
    getChapter(novelId, chapterIndex)
      .then((nextChapter) => {
        if (cancelled) return;
        setChapter(nextChapter); setFacts(nextChapter.facts_status ?? null); setFactsError(null);
        onChapterLoaded(nextChapter);
      }).catch(reason => { if (!cancelled) setFactsError(errorMessage(reason)); });
    return () => { cancelled = true; };
    // The revision invalidates knowledge; only these keys reload prose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelId, chapterIndex, knowledgeRevision]);

  const generation = useRef(0);
  const polling = useRef(false);
  useEffect(() => { generation.current++; }, [novelId, chapterIndex, knowledgeRevision]);
  usePolling(() => {
    if (!chapter || polling.current) return;
    const current = generation.current;
    polling.current = true;
    getChapterFactsStatus(novelId, chapterIndex).then(async (response) => {
      if (current !== generation.current) return;
      const becameReady = response.status.state === "ready" && facts?.state !== "ready";
      setFacts(response.status);
      setFactsError(null);
      if (!becameReady) return;
      // Names are relinked in the same pass, so refresh the prose's spans once.
      setHovered(null);
      const refreshed = await getChapter(novelId, chapterIndex);
      if (current !== generation.current) return;
      setChapter(refreshed); onChapterLoaded(refreshed);
    }).catch((reason) => {
      if (current === generation.current) setFactsError(errorMessage(reason));
    })
      .finally(() => { polling.current = false; });
  }, factsPollInterval(facts), chapter !== null && factsError === null && !factsTerminal(facts));

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
          <ChapterStatus novelId={novelId} chapter={chapterIndex} status={facts} />
          <button type="button" className="chapter-names-toggle" aria-expanded={showNames} onClick={() => setShowNames((open) => !open)}>
            Names{toReview > 0 ? <span className="chapter-names-count">{toReview} to review</span> : null}
          </button>
        </div>
      </header>
      {factsError && <p role="alert" className="reader-records-error">
        Could not load this chapter’s status: {factsError} <button type="button" onClick={() => {
          setFactsError(null);
          void getChapterFactsStatus(novelId, chapterIndex).then((response) => setFacts(response.status))
            .catch((reason) => setFactsError(errorMessage(reason)));
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
            onMouseEnter={() => { if (!clickableEntities) setHovered(index); }}
            onMouseLeave={() => setHovered((current) => current === index ? null : current)}>
            <button
              type="button"
              className={`mention mention-button mention-unlinked${piece.rendering?.status === "locked" ? " mention-confirmed" : ""}`}
              aria-haspopup="dialog"
              aria-label={`Inspect ${piece.text}`}
              onClick={() => setHovered((current) => current === index ? null : index)}
            >{piece.text}</button>
            {hovered === index && (
              <HoverCard
                novelId={novelId}
                rendering={piece.rendering}
                mention={piece.text}
                at={chapter.at}
                onRenderingChanged={(updatedRendering) => applyRendering(updatedRendering, piece.text)}
                onClose={() => setHovered(null)}
              />
            )}
          </span>
        );
      })}
    </div>
  );
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
