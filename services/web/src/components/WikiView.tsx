import { Fragment, useEffect, useState } from "react";
import { getWikiPage, getWikiPages } from "../api";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import type { WikiPageResponse, WikiPagesResponse } from "../types";
import { parseWikiPage, type Inline } from "../wikiMarkdown";

/**
 * Character pages as of the reader's chapter. Each page is the newest version built at or
 * before it, so nothing here comes from a chapter the reader hasn't reached.
 */
export function WikiView({ novelId, onClose }: { novelId: string; at: number; onClose: () => void }) {
  const revision = useKnowledgeRevision(novelId);
  const [list, setList] = useState<WikiPagesResponse | null>(null);
  const [subject, setSubject] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPageResponse | null>(null);
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
    setPage(null);
    getWikiPage(novelId, subject).then((next) => { if (!gone) setPage(next); })
      .catch((reason) => { if (!gone) setError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, subject, revision]);

  return <section className="wiki">
    <div className="wiki-head">
      <h2>Characters</h2>
      {list && <span className="wiki-gate">Up to chapter {list.at}</span>}
      <button type="button" onClick={onClose}>Close</button>
    </div>
    {error && <p role="alert" className="wiki-error">Could not load the wiki: {error}</p>}
    {!list ? !error && <p className="wiki-empty">Loading…</p>
      : !list.pages.length ? <p className="wiki-empty">No character pages yet at your chapter. Pages appear once a character's chapter has its facts.</p>
      : <div className="wiki-body">
          <nav className="wiki-index" aria-label="Characters">
            <ul>{list.pages.map((item) => <li key={item.subject}>
              <button type="button" aria-current={item.subject === subject ? "page" : undefined} onClick={() => setSubject(item.subject)}>
                <span>{item.title}</span><small lang="zh">{item.subject}</small>
              </button>
            </li>)}</ul>
          </nav>
          <article className="wiki-page">
            {!page ? <p className="wiki-empty">Loading page…</p> : <>
              {parseWikiPage(page.body).map((block, index) => {
                switch (block.kind) {
                  case "title": return <h3 key={index} className="wiki-title">{block.text}</h3>;
                  case "heading": return <h4 key={index}>{block.text}</h4>;
                  case "aka": return <p key={index} className="wiki-aka">Also known as <Inlines parts={block.parts} /></p>;
                  case "paragraph": return <p key={index}><Inlines parts={block.parts} /></p>;
                  case "list": return <ul key={index}>{block.items.map((item, i) => <li key={i}><Inlines parts={item} /></li>)}</ul>;
                }
              })}
              <p className="wiki-asof">As of chapter {page.chapter_index}</p>
            </>}
          </article>
        </div>}
  </section>;
}

function Inlines({ parts }: { parts: Inline[] }) {
  return <>{parts.map((part, index) => <Fragment key={index}>
    {part.kind === "bold" ? <strong>{part.text}</strong>
      : part.kind === "cite" ? <span className="wiki-cite" title={`From chapter ${part.chapters}`}>ch. {part.chapters}</span>
      : part.text}
  </Fragment>)}</>;
}
