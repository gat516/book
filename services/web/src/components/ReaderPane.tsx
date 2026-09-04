import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getChapter, getEventStatus, getKnowledgeStatus, putProgress } from "../api";
import type { ChapterFactView, ChapterResponse, EntityView } from "../types";
import { HoverCard } from "./HoverCard";
import { EntityInspector } from "./EntityInspector";
import { usePolling } from "../usePolling";
import { applyRenderingChoices, lastMentionPerEntity, segment } from "../readerSegments";
import { EventList } from "./EventList";

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

export function ReaderPane({ novelId, chapterIndex, clickableEntities, onChapterLoaded, onNoChapter, onOpenRepair }: Props) {
  const [chapter, setChapter] = useState<ChapterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);
  const [selected, setSelected] = useState<{ id: string | null; mention: string } | null>(null);

  // Both hover and click views share only the exact novel/chapter/clearance cache.
  // The server's `at` becomes known on load; changing it discards earlier entity data.
  const cache = useMemo(() => new Map<string, EntityView>(), [novelId, chapterIndex, chapter?.at, chapter?.knowledge?.revision_id, chapter?.knowledge?.version]);

  useEffect(() => {
    setSelected(null);
    setHovered(null);
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
  const needsBindingRefresh = useRef(false);
  useEffect(() => { generation.current++; }, [novelId, chapterIndex]);
  usePolling(() => {
    if (!chapter || polling.current) return;
    const current = generation.current;
    polling.current = true;
    Promise.all([getKnowledgeStatus(novelId, chapterIndex), getEventStatus(novelId, chapterIndex)]).then(async ([status, eventStatus]) => {
      if (current !== generation.current) return;
      if (JSON.stringify(status) === JSON.stringify(chapter.knowledge) &&
          JSON.stringify(eventStatus) === JSON.stringify(chapter.event_knowledge) && !needsBindingRefresh.current) return;
      needsBindingRefresh.current = true;
      // Close old cards immediately; late responses cannot repopulate the new cache.
      cache.clear(); setSelected(null); setHovered(null);
      setChapter(previous => previous ? {...previous, knowledge: status,
        spans: previous.spans.map(span => ({...span, entity_id: null}))} : previous);
      const refreshed = await getChapter(novelId, chapterIndex);
      if (current !== generation.current) return;
      needsBindingRefresh.current = false;
      setChapter(refreshed); onChapterLoaded(refreshed);
    }).catch(() => { /* Keep readable prose; retry the status request next interval. */ })
      .finally(() => { polling.current = false; });
  }, 4000, chapter !== null);

  if (error) return <p className="reader-pane-error">Could not load chapter: {error}</p>;
  if (!chapter) return <p>Loading chapter…</p>;

  // Render every confirmed occurrence with the preferred spelling, while keeping only one
  // interactive anchor per distinct thing. The stored translation remains unchanged.
  const rendered = applyRenderingChoices(chapter.text, chapter.spans);
  const segments = segment(rendered.text, lastMentionPerEntity(rendered.text, rendered.spans));
  const newFactsByEntity = new Map<string, ChapterFactView[]>();
  for (const fact of chapter.new_facts ?? []) {
    const held = newFactsByEntity.get(fact.entity_id);
    if (held) held.push(fact);
    else newFactsByEntity.set(fact.entity_id, [fact]);
  }

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
      {chapter.knowledge?.status === "repair" && <p role="status" className="reader-entity-hint">
        Knowledge cards are under repair. Saved translations are unchanged; unverified facts are withheld.
        {onOpenRepair && <> <button type="button" className="reader-inline-link" onClick={onOpenRepair}>See repair status</button></>}
      </p>}
      {chapter.knowledge?.status === "processing" && <p role="status">Checking names and supported facts…</p>}
      {chapter.knowledge?.status === "failed" && <p role="status">Knowledge processing failed. The chapter is still readable.</p>}
      {chapter.translation_warning?.code === "locked_terms_missing" && <p role="status" className="reader-translation-warning">
        This chapter is readable, but {chapter.translation_warning.term_count} locked name{chapter.translation_warning.term_count === 1 ? " was" : "s were"} not preserved exactly.
      </p>}
      <section className="chapter-events" aria-labelledby="chapter-events-heading">
        <h2 id="chapter-events-heading">What happened</h2>
        <EventList events={chapter.events ?? []} knowledge={chapter.event_knowledge ?? { revision_id: "", version: 0, trusted: false, status: "unavailable" }} />
      </section>
	  {chapter.new_facts.length > 0 && (
		<details className="chapter-events">
		  <summary>Facts extracted from this chapter ({chapter.new_facts.length})</summary>
		  <ul>
			{chapter.new_facts.map((fact) => <li key={`${fact.entity_id}:${fact.attribute}`}>
			  {fact.entity_canonical} — {fact.attribute}: {fact.value}
			</li>)}
		  </ul>
		</details>
	  )}
      {clickableEntities && chapter.spans.length === 0 && <p className="reader-entity-hint">
        No named mentions are available for this chapter yet. Cards do not require facts or a glossary entry.
      </p>}
      {segments.map((piece, index) => {
        if (!piece.mention) return <span key={index}>{piece.text}</span>;
        const newFacts = piece.entityId ? newFactsByEntity.get(piece.entityId) ?? [] : [];
        return (
          <span className="mention-anchor" key={index}
            onMouseEnter={() => { if (!clickableEntities && !selected) setHovered(index); }}
            onMouseLeave={() => setHovered((current) => current === index ? null : current)}>
            <button
              type="button"
              className={`mention mention-button${piece.entityId ? "" : " mention-unlinked"}${piece.rendering?.status === "locked" ? " mention-confirmed" : ""}${newFacts.length ? " mention-has-new-fact" : ""}`}
              aria-haspopup="dialog"
              aria-label={newFacts.length
                ? `Inspect ${piece.text} — ${newFacts.length} fact${newFacts.length > 1 ? "s" : ""} learned in this chapter`
                : `Inspect ${piece.text}`}
              onClick={() => { setHovered(null); setSelected({ id: piece.entityId, mention: piece.text }); }}
            >{piece.text}</button>
            {newFacts.length > 0 && (
              <span className="mention-new-fact" role="note">
                <span className="mention-new-fact-label">New this chapter</span>
                {newFacts.map((fact) => (
                  <span className="mention-new-fact-item" key={fact.attribute}>
                    {fact.attribute} — {fact.value}
                  </span>
                ))}
              </span>
            )}
            {hovered === index && (
              <HoverCard
                novelId={novelId}
                entityId={piece.entityId}
                rendering={piece.rendering}
                status={chapter.knowledge?.status}
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
        status={chapter.knowledge?.status}
        mention={selected.mention}
        at={chapter.at}
        cache={cache}
        onClose={() => setSelected(null)}
      />}
    </div>
  );
}
