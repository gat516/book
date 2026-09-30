import { useEffect, useMemo, useState } from "react";
import { ArrowUpRight, BookOpen, List, MessageCircle, Plus, ShieldCheck } from "lucide-react";
import { ChoiceTabs, Fade } from "./animate-ui/motion";
import { getWikiPage, getWikiPages } from "../api";
import { useKnowledgeRevision } from "../knowledgeUpdates";
import type { ChapterResponse, WikiPageResponse, WikiPageSummary } from "../types";
import { AskBox } from "./AskBox";
import { ReaderPane } from "./ReaderPane";
import { ProgressControls } from "./ProgressControls";
import { TranslationNotice } from "./TranslationNotice";

interface Props {
  novelId: string;
  chapterIndex: number;
  chapter: ChapterResponse | null;
  clickableEntities: boolean;
  onChapterLoaded: (chapter: ChapterResponse) => void;
  onNoChapter: () => void;
  onNavigate: (chapter: number) => Promise<void>;
  onFindMore: () => Promise<void>;
  onChapters: () => void;
  onAdd: () => void;
  onOpenWiki: (subject?: string) => void;
}

export function ReadingDesk(props: Props) {
  const { novelId, chapterIndex, chapter, onOpenWiki } = props;
  // §0.3: a reread deliberately asks for less than the stored clearance. The server
  // still authorizes every request; no future facts are fetched and hidden in CSS.
  const at = chapter ? Math.min(chapter.at, chapterIndex) : null;
  const revision = useKnowledgeRevision(novelId);
  const [pages, setPages] = useState<WikiPageSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (at === null) return;
    let gone = false;
    setLoading(true); setError(null); setPages([]);
    getWikiPages(novelId, at).then(result => { if (!gone) setPages(result.pages); })
      .catch(reason => { if (!gone) setError(String(reason)); })
      .finally(() => { if (!gone) setLoading(false); });
    return () => { gone = true; };
  }, [novelId, at, revision, chapter?.facts_status?.state, retry]);
  const ordered = useMemo(() => {
    const mentioned = new Set(chapter?.spans.map(span => span.rendering?.source_term).filter(Boolean));
    return [...pages].sort((a, b) => Number(mentioned.has(b.source_term)) - Number(mentioned.has(a.source_term)));
  }, [chapter?.spans, pages]);
  return <div className="reading-desk">
    <div className="reading-column">
      <TranslationNotice novelId={novelId} />
      <div className="reading-paper">
        <ReaderPane novelId={novelId} chapterIndex={chapterIndex} clickableEntities={props.clickableEntities}
          onChapterLoaded={props.onChapterLoaded} onNoChapter={props.onNoChapter}
          wikiPages={pages} wikiLoading={loading} wikiError={error} onOpenWiki={onOpenWiki} />
        <ProgressControls chapterIndex={chapterIndex} hasNext={chapter?.has_next ?? false}
          onNavigate={props.onNavigate} sourceURL={chapter?.source_url} onFindMore={props.onFindMore} />
      </div>
      <div className="reading-bottom-actions"><button className="text-button" onClick={props.onChapters}><List size={15} />All chapters</button><button className="text-button" onClick={props.onAdd}><Plus size={15} />Add chapter</button></div>
    </div>
    {at !== null && <StoryCompanion key={`${novelId}:${at}`} novelId={novelId} at={at} pages={ordered}
      loading={loading} error={error} revision={revision} onRetry={() => setRetry(n => n + 1)} onOpenWiki={onOpenWiki} />}
  </div>;
}

export function StoryCompanion({ novelId, at, pages, loading, error, revision, onRetry, onOpenWiki }: {
  novelId: string; at: number; pages: WikiPageSummary[]; loading: boolean; error: string | null;
  revision: number; onRetry: () => void; onOpenWiki: (subject?: string) => void;
}) {
  const [tab, setTab] = useState<"wiki" | "ask">("wiki");
  const [selected, setSelected] = useState<string | null>(null);
  const subject = pages.find(page => page.subject === selected)?.subject ?? pages[0]?.subject;
  const [page, setPage] = useState<WikiPageResponse | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  useEffect(() => {
    setPage(null); setPageError(null);
    if (!subject || tab !== "wiki") return;
    let gone = false;
    getWikiPage(novelId, subject, at).then(result => { if (!gone) setPage(result); })
      .catch(reason => { if (!gone) setPageError(String(reason)); });
    return () => { gone = true; };
  }, [novelId, at, subject, tab, revision, pages]);
  // Before the index loads, both subject values are undefined. Require an actual
  // page before comparing them or reading its chapter boundary (§0.3).
  const current = page !== null && page.subject === subject && page.at <= at ? page : null;
  return <aside className="story-companion" aria-label="Story companion">
    <ChoiceTabs className="companion-tabs" label="Companion view" value={tab} onChange={setTab}
      choices={[{ value: "wiki", label: "Story wiki", icon: <BookOpen size={15} /> }, { value: "ask", label: "Ask AI", icon: <MessageCircle size={15} /> }]} />
    <p className="companion-boundary"><ShieldCheck size={13} />Knowledge through chapter {at}</p>
    {tab === "ask" ? <Fade key="ask" className="companion-content companion-content-ask"><AskBox key={`${novelId}:${at}`} novelId={novelId} at={at} /></Fade>
      : <Fade key="wiki" className="companion-content">
        {loading ? <p className="companion-note" role="status">Loading wiki…</p>
          : error ? <p className="companion-note" role="alert">Could not load the wiki. <button className="text-button" onClick={onRetry}>Retry</button></p>
          : !pages.length ? <><span className="character-monogram" aria-hidden="true"><BookOpen /></span><h2>No wiki pages yet</h2><p className="companion-note">Build reader features to add people and places.</p></>
          : <>
            <label className="companion-select"><span className="visually-hidden">Wiki page</span><select value={subject} onChange={event => setSelected(event.target.value)}>{pages.map(item => <option key={item.subject} value={item.subject}>{item.title} · {item.kind}</option>)}</select></label>
            {pageError ? <p role="alert" className="companion-note">Could not open this page. <button className="text-button" onClick={onRetry}>Retry</button></p>
              : !current ? <p role="status" className="companion-note">Loading page…</p>
              : <><div className="character-monogram" aria-hidden="true">{current.title.slice(0, 1)}</div><p className="companion-kind">{current.kind}</p><h2>{current.title}</h2>
                <ul className="companion-facts">{current.facts.slice(0, 3).map(fact => <li key={`${fact.chapter}:${fact.version}:${fact.ordinal}`}>{fact.text}<span>LEARNED IN CHAPTER {fact.chapter}</span></li>)}</ul>
                <button className="companion-open" onClick={() => onOpenWiki(current.subject)}>Open full wiki<ArrowUpRight size={15} /></button>
              </>}
          </>}
      </Fade>}
  </aside>;
}
