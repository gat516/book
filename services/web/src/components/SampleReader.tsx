import { publicHref } from "../publicNavigation";
import { useState } from "react";
import { demoChapters, demoFacts, type DemoPerson } from "../demo";
import { SiteFooter, SiteHeader } from "./SiteHeader";
import { Fade } from "./animate-ui/motion";

export function SampleReader() {
  const [index, setIndex] = useState(0);
  const [person, setPerson] = useState<DemoPerson>("Mei");
  const [panel, setPanel] = useState<"wiki" | "ask">("wiki");
  const [source, setSource] = useState(false);
  const [answer, setAnswer] = useState(false);
  const chapter = demoChapters[index];
  function go(next: number) { setIndex(next); setAnswer(false); }
  function openPerson(next: DemoPerson) { setPerson(next); setPanel("wiki"); }
  return <><SiteHeader><a className="site-signin" href={publicHref("/")}>Your library →</a></SiteHeader><main id="main-content" className="sample-page">
    <div className="sample-intro"><span className="sample-badge">Interactive demo</span><p>An original story with prepared translations and answers. No sign-in, API key, or live AI calls.</p></div>
    <header className="sample-heading"><p className="eyebrow">The QiReadr bookshelf / 001</p><h1>The Lantern Keeper</h1><p>A forgotten tower. A copper key. A light that remembers.</p></header>
    <div className="sample-tip"><span aria-hidden="true">✦</span><p>Try this: select <strong>Mei</strong> in the story, then open chapter 2. Her character card learns something new.</p></div>
    <nav className="sample-chapters" aria-label="Sample chapters">{demoChapters.map((item, n) => <button key={item.title} aria-pressed={index === n} onClick={() => go(n)}><span>0{n + 1}</span>{item.title}</button>)}</nav>
    <div className="sample-layout">
      <article className="sample-prose" aria-label="Sample chapter"><header><p className="eyebrow">Chapter {index + 1} of 3</p><button className="text-button" aria-pressed={source} onClick={() => setSource(!source)}>{source ? "Read in English" : "View Chinese text"}</button></header><h2>{chapter.title}</h2>
        <Fade key={`${index}-${source}`} lang={source ? "zh" : "en"}>{(source ? chapter.source : chapter.paragraphs).map((paragraph, n) => <p key={`${index}-${source}-${n}`}>{source ? paragraph : paragraph.split(/\b(Lin|Mei)\b/g).map((part, k) => part === "Lin" || part === "Mei" ? <button key={k} className="sample-mention" aria-label={`Show ${part}’s character card`} onClick={() => openPerson(part)}>{part}</button> : part)}</p>)}</Fade>
        <div className="sample-reading-nav"><button disabled={index === 0} onClick={() => go(index - 1)}>← Previous</button>{index < 2 ? <button className="btn-primary" onClick={() => go(index + 1)}>Next chapter →</button> : <a className="action-link btn-primary" href={publicHref("/")}>Start your own library →</a>}</div>
      </article>
      <aside className="sample-companion" aria-label="Story companion"><div className="sample-panel-tabs" role="group" aria-label="Companion view"><button aria-pressed={panel === "wiki"} onClick={() => setPanel("wiki")}>Character wiki</button><button aria-pressed={panel === "ask"} onClick={() => setPanel("ask")}>Ask the story</button></div>
        <div className="sample-knowledge-limit">Knowledge through chapter {index + 1}</div>
        {panel === "wiki" ? <div className="sample-character" aria-live="polite"><div className="sample-people" role="group" aria-label="Characters">{(["Mei", "Lin"] as const).map(name => <button key={name} aria-pressed={person === name} onClick={() => setPerson(name)}>{name}</button>)}</div><span className="character-monogram" aria-hidden="true">{person === "Mei" ? "梅" : "林"}</span><h2>{person}</h2><p className="sample-character-label">Character · {person === "Mei" ? "梅" : "林"}</p><ul className="sample-facts">{demoFacts(person, index + 1).map(fact => <li key={fact.text}>{fact.text}<span>Learned in chapter {fact.chapter}</span></li>)}</ul><p className="sample-boundary-note">{index < 2 ? "Later discoveries stay hidden. Advance a chapter to learn more." : "You’ve reached the end of this sample."}</p></div>
        : <div className="sample-ask"><h2>Ask about what you’ve read.</h2><p>Try a prepared question for this chapter.</p><button className="sample-question" onClick={() => setAnswer(true)}>{chapter.question} <span aria-hidden="true">↗</span></button>{answer && <div className="sample-answer" role="status"><p>{chapter.answer}</p><span>Source: chapter {index + 1}</span></div>}<p className="sample-boundary-note">These are written sample answers. In your own books, Ask AI uses your chosen model and the chapters you’ve reached.</p></div>}
      </aside>
    </div>
    <section className="sample-outro"><div><h2>A companion for your next book.</h2><p>Your library starts empty and stays private. We’ll walk you through adding your first story.</p></div><a className="action-link btn-primary" href={publicHref("/")}>Get started →</a></section>
  </main><SiteFooter /></>;
}
