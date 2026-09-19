import { useEffect, useState } from "react";
import { getWikiPage, getWikiPages } from "../api";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import type { WikiPageResponse, WikiPagesResponse } from "../types";
import { buildWikiPage, type Entry } from "../wikiPage";

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

  useEffect(() => {
    let gone = false;
    setList(null); setError(null);
    getWikiPages(novelId).then((next) => {
      if (gone) return;
      setList(next);
      setSubject((current) => current && next.pages.some((p) => p.subject === current) ? current : next.pages[0]?.subject ?? null);
    }).catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, revision]);

  useEffect(() => {
    if (!subject) { setPage(null); return; }
    let gone = false;
    setPage(null); setTab("page");
    getWikiPage(novelId, subject).then((next) => { if (!gone) setPage(next); })
      .catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, subject, revision]);

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
                <button type="button" role="tab" aria-selected={tab === "more"} onClick={() => setTab("more")}>More{model.more.length ? ` (${model.more.length})` : ""}</button>
              </div>
              {tab === "page" ? <>
                {model.aliases.length > 0 && <Section heading="Also known as" entries={model.aliases} />}
                {model.intro.length > 0 && <Section heading="Introduction" entries={model.intro} />}
                {model.relationships.length > 0 && <>
                  <h4>Relationships</h4>
                  <dl className="wiki-relations">{model.relationships.map((group) => <div key={group.heading}>
                    <dt>{group.heading}</dt>
                    <dd>{group.people.map((person, i) => <span key={i} title={person.text}>
                      {known.has(person.subject)
                        ? <button type="button" className="wiki-link" onClick={() => setSubject(person.subject)}>{person.name}</button>
                        : person.name}
                      <Cite chapter={person.chapter} />{i < group.people.length - 1 ? ", " : ""}
                    </span>)}</dd>
                  </div>)}</dl>
                </>}
                {model.sections.map((section) => <Section key={section.heading} heading={section.heading} entries={section.entries} list />)}
                {model.history.length > 0 && <Section heading="History" entries={model.history} />}
              </> : model.more.length
                ? <Section heading="Other relationships" entries={model.more} list />
                : <p className="wiki-empty">Nothing else yet.</p>}
            </>}
          </article>
        </div>}
  </section>;
}

function Section({ heading, entries, list = false }: { heading: string; entries: Entry[]; list?: boolean }) {
  return <>
    <h4>{heading}</h4>
    {list
      ? <ul>{entries.map((entry, i) => <li key={i}>{entry.text}<Cite chapter={entry.chapter} /></li>)}</ul>
      : <p>{entries.map((entry, i) => <span key={i}>{entry.text}<Cite chapter={entry.chapter} />{" "}</span>)}</p>}
  </>;
}

function Cite({ chapter }: { chapter: number }) {
  return <span className="wiki-cite" title={`From chapter ${chapter}`}>ch. {chapter}</span>;
}
