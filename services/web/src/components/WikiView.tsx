import { useEffect, useState, type ReactNode } from "react";
import { getWikiPage, getWikiPages, retractFact } from "../api";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import type { WikiPageResponse, WikiPagesResponse } from "../types";
import { buildWikiPage, type Entry, type FactRef } from "../wikiPage";

/**
 * Character pages as of the reader's chapter, assembled from the facts learned so far.
 * Nothing here comes from a chapter the reader hasn't reached.
 */
export function WikiView({ novelId, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const revision = useKnowledgeRevision(novelId);
  const [list, setList] = useState<WikiPagesResponse | null>(null);
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
      setSubject((current) => current && next.pages.some((p) => p.subject === current) ? current : next.pages[0]?.subject ?? null);
    }).catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, revision, reload]);

  useEffect(() => {
    if (!subject) { setPage(null); return; }
    let gone = false;
    getWikiPage(novelId, subject).then((next) => { if (!gone) setPage(next); })
      .catch((reason) => { if (!gone) { setPage(null); setError(String(reason)); } });
    return () => { gone = true; };
  }, [novelId, subject, revision, reload]);

  useEffect(() => { setTab("page"); setSelected(null); }, [subject]);

  const known = new Set(list?.pages.map((p) => p.subject));
  const model = page && buildWikiPage(page.subject, page.facts, page.names);
  return <section className="wiki">
    <div className="wiki-head">
      <h2>Characters</h2>
      {list && <span className="wiki-gate">Up to chapter {list.at}</span>}
      <button type="button" onClick={onClose}>Close</button>
    </div>
    {error && <p role="alert" className="wiki-error">Could not load the wiki: {error}</p>}
    {!list ? !error && <p className="wiki-empty">Loading…</p>
      : !list.pages.length ? <p className="wiki-empty">No characters yet at your chapter. A character appears once a chapter's facts name them.</p>
      : <div className="wiki-body">
          <nav className="wiki-index" aria-label="Characters">
            <ul>{list.pages.map((item) => <li key={item.subject}>
              <button type="button" aria-current={item.subject === subject ? "page" : undefined} onClick={() => setSubject(item.subject)}>
                <span>{item.title}</span><small>{item.facts} fact{item.facts === 1 ? "" : "s"}</small>
              </button>
            </li>)}</ul>
          </nav>
          <article className="wiki-page">
            {!page || !model ? <p className="wiki-empty">Loading page…</p> : <>
              <h3 className="wiki-title">{page.title}</h3>
              <div className="wiki-tabs" role="tablist">
                <button type="button" role="tab" aria-selected={tab === "page"} onClick={() => setTab("page")}>Page</button>
                <button type="button" role="tab" aria-selected={tab === "more"} onClick={() => setTab("more")}>More{model.more.length + model.mentions.length ? ` (${model.more.length + model.mentions.length})` : ""}</button>
              </div>
              {tab === "page" ? <>
                {model.aliases.length > 0 && <Section heading="Also known as" entries={model.aliases} {...pick} />}
                {model.intro.length > 0 && <Section heading="Introduction" entries={model.intro} {...pick} />}
                {model.relationships.length > 0 && <>
                  <h4>Relationships</h4>
                  <dl className="wiki-relations">{model.relationships.map((group) => <div key={group.heading}>
                    <dt>{group.heading}</dt>
                    <dd>{group.people.map((person, i) => <span key={key(person.ref)}>
                      <Fact entry={{ text: "", chapter: person.chapter, ref: person.ref }} title={person.text} {...pick}>
                        {known.has(person.subject)
                          ? <button type="button" className="wiki-link" onClick={(event) => { event.stopPropagation(); setSubject(person.subject); }}>{person.name}</button>
                          : person.name}
                      </Fact>{i < group.people.length - 1 ? ", " : ""}
                    </span>)}</dd>
                  </div>)}</dl>
                </>}
                {model.sections.map((section) => <Section key={section.heading} heading={section.heading} entries={section.entries} list {...pick} />)}
                {model.history.length > 0 && <Section heading="History" entries={model.history} {...pick} />}
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

function Section({ heading, entries, list = false, ...pick }: { heading: string; entries: Entry[]; list?: boolean } & Pick) {
  return <>
    <h4>{heading}</h4>
    {list
      ? <ul>{entries.map((entry) => <li key={key(entry.ref)}><Fact entry={entry} {...pick} /></li>)}</ul>
      : <p>{entries.map((entry) => <span key={key(entry.ref)}><Fact entry={entry} {...pick} />{" "}</span>)}</p>}
  </>;
}

function key(ref: FactRef): string {
  return `${ref.chapter}:${ref.version}:${ref.ordinal}`;
}

// A fact is selected by clicking it (or Enter/Space); a selected fact offers Remove,
// which retracts it for every reader, and Keep. Escape or a second click deselects.
function Fact({ entry, title, children, selected, onSelect, onRemove }:
  { entry: Entry; title?: string; children?: ReactNode } & Pick) {
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
      {children ?? entry.text}<Cite chapter={entry.chapter} />
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
