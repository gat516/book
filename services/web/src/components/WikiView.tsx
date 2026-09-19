import { Fragment, useEffect, useState, type ReactNode } from "react";
import { getWikiEvents, getWikiPage, getWikiPages, retractFact } from "../api";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import type { SubjectKind, WikiEventsResponse, WikiPageResponse, WikiPagesResponse } from "../types";
import { buildSubjectPage, buildWikiPage, PAGE_ORDER, type Entry, type FactRef } from "../wikiPage";

type Shelf = SubjectKind | "events";
const SHELVES: { shelf: Shelf; label: string; empty: string }[] = [
  { shelf: "character", label: "Characters", empty: "No characters yet at your chapter. A character appears once a chapter's facts name them." },
  { shelf: "organization", label: "Organizations", empty: "No organizations yet at your chapter: sects, teams and other factions appear once a chapter's facts name them." },
  { shelf: "place", label: "Places", empty: "No places yet at your chapter." },
  { shelf: "item", label: "Items", empty: "No items yet at your chapter." },
  { shelf: "events", label: "Events", empty: "No events yet at your chapter." },
];

/**
 * Wiki pages -- characters, organizations, places, items -- and an events timeline, as
 * of the reader's chapter, assembled from the facts learned so far. Nothing here comes
 * from a chapter the reader hasn't reached.
 */
export function WikiView({ novelId, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const revision = useKnowledgeRevision(novelId);
  const [list, setList] = useState<WikiPagesResponse | null>(null);
  const [shelf, setShelf] = useState<Shelf>("character");
  const [events, setEvents] = useState<WikiEventsResponse | null>(null);
  const [subject, setSubject] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPageResponse | null>(null);
  const [tab, setTab] = useState<"page" | "more">("page");
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // The one fact a reader has selected, ready to remove.
  const [selected, setSelected] = useState<string | null>(null);

  // Remove a bad fact for every reader. The fact is kept, marked retracted (0113).
  async function remove(ref: FactRef) {
    try {
      await retractFact(novelId, ref);
      setSelected(null);
      setReload((n) => n + 1);
    } catch (reason) {
      setError(String(reason));
    }
  }
  const pick = { selected, onSelect: setSelected, onRemove: remove };

  useEffect(() => {
    let gone = false;
    setList(null); setError(null);
    getWikiPages(novelId).then((next) => {
      if (gone) return;
      setList(next);
    }).catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, revision, reload]);

  // Keep the open page when it is on this shelf; otherwise open the shelf's first page.
  useEffect(() => {
    if (!list || shelf === "events") return;
    const onShelf = list.pages.filter((p) => p.kind === shelf);
    setSubject((current) => current && onShelf.some((p) => p.subject === current) ? current : onShelf[0]?.subject ?? null);
  }, [list, shelf]);

  useEffect(() => {
    if (shelf !== "events") return;
    let gone = false;
    getWikiEvents(novelId).then((next) => { if (!gone) setEvents(next); })
      .catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, shelf, revision, reload]);

  useEffect(() => {
    if (!subject) { setPage(null); return; }
    let gone = false;
    getWikiPage(novelId, subject).then((next) => { if (!gone) setPage(next); })
      .catch((reason) => { if (!gone) { setPage(null); setError(String(reason)); } });
    return () => { gone = true; };
  }, [novelId, subject, revision, reload]);

  useEffect(() => { setTab("page"); setSelected(null); }, [subject]);

  const kindOf = new Map(list?.pages.map((p) => [p.subject, p.kind]));
  const known = new Set(kindOf.keys());
  // Open another subject's page, on its own shelf.
  const open = (id: string) => { const kind = kindOf.get(id); if (kind) { setShelf(kind); setSubject(id); } };
  const current = SHELVES.find((s) => s.shelf === shelf)!;
  const shelfPages = list?.pages.filter((p) => p.kind === shelf) ?? [];
  const model = page && page.kind === "character" && page.subject === subject ? buildWikiPage(page.subject, page.facts, page.names) : null;
  const subjectModel = page && page.kind !== "character" && page.subject === subject ? buildSubjectPage(page.subject, page.kind, page.facts) : null;
  return <section className="wiki">
    <div className="wiki-head">
      <h2>Wiki</h2>
      {list && <span className="wiki-gate">Up to chapter {list.at}</span>}
      <button type="button" onClick={onClose}>Close</button>
    </div>
    <div className="wiki-shelves wiki-tabs" role="tablist" aria-label="Wiki sections">
      {SHELVES.map((s) => <button key={s.shelf} type="button" role="tab" aria-selected={shelf === s.shelf}
        onClick={() => setShelf(s.shelf)}>{s.label}</button>)}
    </div>
    {error && <p role="alert" className="wiki-error">Could not load the wiki: {error}</p>}
    {shelf === "events" ? <Events data={events} empty={current.empty} known={known} onOpen={open} {...pick} />
      : !list ? !error && <p className="wiki-empty">Loading…</p>
      : !shelfPages.length ? <p className="wiki-empty">{current.empty}</p>
      : <div className="wiki-body">
          <nav className="wiki-index" aria-label={current.label}>
            <ul>{shelfPages.map((item) => <li key={item.subject}>
              <button type="button" aria-current={item.subject === subject ? "page" : undefined} onClick={() => setSubject(item.subject)}>
                <span>{item.title}</span><small>{item.facts} fact{item.facts === 1 ? "" : "s"}</small>
              </button>
            </li>)}</ul>
          </nav>
          <article className="wiki-page">
            {subjectModel && page ? <>
              <h3 className="wiki-title">{page.title}</h3>
              {subjectModel.intro.length > 0 && <Section heading="Introduction" entries={subjectModel.intro} {...pick} />}
              {subjectModel.aliases.length > 0 && <Section heading="Also known as" entries={subjectModel.aliases} {...pick} />}
              {subjectModel.details.length > 0 && <Section heading="Details" entries={subjectModel.details} list {...pick} />}
              {subjectModel.ties && <Section heading={subjectModel.ties.heading} entries={subjectModel.ties.entries} list {...pick} />}
              {subjectModel.history.length > 0 && <History entries={subjectModel.history} {...pick} />}
            </> : !page || !model ? <p className="wiki-empty">Loading page…</p> : <>
              <h3 className="wiki-title">{page.title}</h3>
              <div className="wiki-tabs" role="tablist">
                <button type="button" role="tab" aria-selected={tab === "page"} onClick={() => setTab("page")}>Page</button>
                <button type="button" role="tab" aria-selected={tab === "more"} onClick={() => setTab("more")}>More{model.more.length + model.mentions.length ? ` (${model.more.length + model.mentions.length})` : ""}</button>
              </div>
              {tab === "page" ? <>
                {PAGE_ORDER.map((part) => {
                  switch (part) {
                    case "intro": return model.intro.length > 0 && <Section key={part} heading="Introduction" entries={model.intro} {...pick} />;
                    case "alias": return model.aliases.length > 0 && <Section key={part} heading="Also known as" entries={model.aliases} {...pick} />;
                    case "history": return model.history.length > 0 && <History key={part} entries={model.history} {...pick} />;
                    case "relationships": return model.relationships.length > 0 && <Fragment key={part}>
                      <h4>Relationships</h4>
                      <dl className="wiki-relations">{model.relationships.map((group) => <div key={group.heading}>
                        <dt>{group.heading}</dt>
                        <dd>{group.people.map((person, i) => <span key={key(person.ref)}>
                          <Fact entry={{ text: "", chapter: person.chapter, ref: person.ref }} title={person.text} {...pick}>
                            {known.has(person.subject)
                              ? <button type="button" className="wiki-link" onClick={(event) => { event.stopPropagation(); open(person.subject); }}>{person.name}</button>
                              : person.name}
                          </Fact>{i < group.people.length - 1 ? ", " : ""}
                        </span>)}</dd>
                      </div>)}</dl>
                    </Fragment>;
                    default: {
                      const section = model.sections.find((s) => s.category === part);
                      return section && <Section key={part} heading={section.heading} entries={section.entries} list {...pick} />;
                    }
                  }
                })}
              </> : model.more.length + model.mentions.length ? <>
                {model.more.length > 0 && <Section heading="Other relationships" entries={model.more} list {...pick} />}
                {model.mentions.length > 0 && <Section heading="Mentioned in" entries={model.mentions} list {...pick} />}
              </> : <p className="wiki-empty">Nothing else yet.</p>}
            </>}
          </article>
        </div>}
  </section>;
}

interface Pick { selected: string | null; onSelect: (key: string | null) => void; onRemove: (ref: FactRef) => void }

// The whole book's events, grouped by chapter, each linked to the pages of what it names.
function Events({ data, empty, known, onOpen, ...pick }:
  { data: WikiEventsResponse | null; empty: string; known: Set<string>; onOpen: (id: string) => void } & Pick) {
  if (!data) return <p className="wiki-empty">Loading…</p>;
  if (!data.facts.length) return <p className="wiki-empty">{empty}</p>;
  const chapters = [...new Set(data.facts.map((fact) => fact.chapter))];
  return <article className="wiki-page wiki-events">
    <div className="wiki-history">{chapters.map((chapter) => <section key={chapter}>
      <h5>Chapter {chapter}</h5>
      <ul>{data.facts.filter((fact) => fact.chapter === chapter).map((fact) => {
        const ref = { chapter: fact.chapter, version: fact.version, ordinal: fact.ordinal };
        const linked = [...new Set(fact.subjects)].filter((id) => known.has(id));
        return <li key={key(ref)}>
          <Fact entry={{ text: fact.text, chapter: fact.chapter, ref }} cite={false} {...pick} />
          {linked.length > 0 && <span className="wiki-involves">{linked.map((id) =>
            <button key={id} type="button" className="wiki-link" onClick={() => onOpen(id)}>{data.names[id]}</button>)}</span>}
        </li>;
      })}</ul>
    </section>)}</div>
  </article>;
}

function Section({ heading, entries, list = false, ...pick }: { heading: string; entries: Entry[]; list?: boolean } & Pick) {
  return <>
    <h4>{heading}</h4>
    {list
      ? <ul>{entries.map((entry) => <li key={key(entry.ref)}><Fact entry={entry} {...pick} /></li>)}</ul>
      : <p>{entries.map((entry) => <span key={key(entry.ref)}><Fact entry={entry} {...pick} />{" "}</span>)}</p>}
  </>;
}

// History grows every chapter, so it's a timeline: one group per chapter, one bullet
// per fact, with the chapter shown once on the group instead of after every fact.
function History({ entries, ...pick }: { entries: Entry[] } & Pick) {
  const chapters = [...new Set(entries.map((entry) => entry.chapter))];
  return <>
    <h4>History</h4>
    <div className="wiki-history">{chapters.map((chapter) => <section key={chapter}>
      <h5>Chapter {chapter}</h5>
      <ul>{entries.filter((entry) => entry.chapter === chapter).map((entry) =>
        <li key={key(entry.ref)}><Fact entry={entry} cite={false} {...pick} /></li>)}</ul>
    </section>)}</div>
  </>;
}

function key(ref: FactRef): string {
  return `${ref.chapter}:${ref.version}:${ref.ordinal}`;
}

// A fact is selected by clicking it (or Enter/Space); a selected fact offers Remove,
// which retracts it for every reader, and Keep. Escape or a second click deselects.
function Fact({ entry, title, children, cite = true, selected, onSelect, onRemove }:
  { entry: Entry; title?: string; children?: ReactNode; cite?: boolean } & Pick) {
  const id = key(entry.ref);
  const isSelected = selected === id;
  const toggle = () => onSelect(isSelected ? null : id);
  return <>
    <span className={`wiki-fact${isSelected ? " is-selected" : ""}`} role="button" tabIndex={0} aria-pressed={isSelected}
      title={title} onClick={toggle}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
        else if (event.key === "Escape") onSelect(null);
      }}>
      {children ?? entry.text}{cite && <Cite chapter={entry.chapter} />}
    </span>
    {isSelected && <span className="wiki-fact-actions">
      <button type="button" className="btn-danger" onClick={() => onRemove(entry.ref)}>Remove</button>
      <button type="button" onClick={() => onSelect(null)}>Keep</button>
    </span>}
  </>;
}

function Cite({ chapter }: { chapter: number }) {
  return <span className="wiki-cite" title={`From chapter ${chapter}`}>ch. {chapter}</span>;
}
