import { publicHref } from "../publicNavigation";
import { useLayoutEffect, useState } from "react";
import { demoChapters, demoFacts, type DemoPerson } from "../demo";
import { SiteFooter, SiteHeader } from "./SiteHeader";
import { ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, Languages, MessageCircle, ShieldCheck } from "lucide-react";
import { Button, ChoiceTabs, Fade } from "./animate-ui/motion";

export function SampleReader() {
  const [index, setIndex] = useState(0);
  const [person, setPerson] = useState<DemoPerson>("Mei");
  const [panel, setPanel] = useState<"wiki" | "ask">("wiki");
  const [source, setSource] = useState(false);
  const [answer, setAnswer] = useState(false);
  const chapter = demoChapters[index];
  useLayoutEffect(() => {
    if (window.innerWidth <= 760) {
      const frame = requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "instant" }));
      return () => cancelAnimationFrame(frame);
    }
  }, [index]);
  function go(next: number) { setIndex(next); setAnswer(false); }
  function openPerson(next: DemoPerson) { setPerson(next); setPanel("wiki"); }
  return <><SiteHeader><a className="site-signin" href={publicHref("/")}>Library <ArrowRight size={15} /></a></SiteHeader><main id="main-content" className="sample-page">
    <div className="sample-intro"><span className="sample-badge">Interactive demo</span><p>Sample story · Prepared answers · No AI calls</p></div>
    <header className="sample-heading"><p className="eyebrow">The QiReadr bookshelf / 001</p><h1>The Lantern Keeper</h1><p>A forgotten tower. A copper key. A light that remembers.</p></header>
    <nav className="sample-chapters" aria-label="Sample chapters">{demoChapters.map((item, n) => <button key={item.title} aria-pressed={index === n} onClick={() => go(n)}><span>0{n + 1}</span>{item.title}</button>)}</nav>
    <div className="sample-layout">
      <article className="sample-prose" aria-label="Sample chapter"><header><p className="eyebrow">Chapter {index + 1} of 3</p><button className="text-button" aria-pressed={source} onClick={() => setSource(!source)}><Languages size={15} />{source ? "Read in English" : "View Chinese text"}</button></header><h2>{chapter.title}</h2>
        <Fade key={`${index}-${source}`} lang={source ? "zh" : "en"}>{(source ? chapter.source : chapter.paragraphs).map((paragraph, n) => <p key={`${index}-${source}-${n}`}>{source ? paragraph : paragraph.split(/\b(Lin|Mei)\b/g).map((part, k) => part === "Lin" || part === "Mei" ? <button key={k} className="sample-mention" aria-label={`Show ${part}’s character card`} onClick={() => openPerson(part)}>{part}</button> : part)}</p>)}</Fade>
        <div className="sample-reading-nav"><Button disabled={index === 0} onClick={() => go(index - 1)}><ArrowLeft size={15} />Previous</Button>{index < 2 ? <Button className="btn-primary" onClick={() => go(index + 1)}>Next chapter<ArrowRight size={15} /></Button> : <a className="action-link btn-primary" href={publicHref("/")}>Open library<ArrowRight size={15} /></a>}</div>
      </article>
      <aside className="sample-companion" aria-label="Story companion"><ChoiceTabs className="sample-panel-tabs" label="Companion view" value={panel} onChange={setPanel} choices={[{ value: "wiki", label: "Character wiki", icon: <BookOpen size={15} /> }, { value: "ask", label: "Ask the story", icon: <MessageCircle size={15} /> }]} />
        <div className="sample-knowledge-limit"><ShieldCheck size={13} />Knowledge through chapter {index + 1}</div>
        {panel === "wiki" ? <Fade key="wiki" className="sample-character" aria-live="polite"><div className="sample-people" role="group" aria-label="Characters">{(["Mei", "Lin"] as const).map(name => <button key={name} aria-pressed={person === name} onClick={() => setPerson(name)}>{name}</button>)}</div><span className="character-monogram" aria-hidden="true">{person === "Mei" ? "梅" : "林"}</span><h2>{person}</h2><p className="sample-character-label">Character · {person === "Mei" ? "梅" : "林"}</p><ul className="sample-facts">{demoFacts(person, index + 1).map(fact => <li key={fact.text}>{fact.text}<span>Learned in chapter {fact.chapter}</span></li>)}</ul></Fade>
        : <Fade key="ask" className="sample-ask"><button className="sample-question" onClick={() => setAnswer(true)}>{chapter.question} <ArrowUpRight size={15} /></button>{answer && <Fade rise={5} className="sample-answer" role="status"><p>{chapter.answer}</p><span>Source: chapter {index + 1}</span></Fade>}</Fade>}
      </aside>
    </div>
    <section className="sample-outro"><div><h2>Keep reading.</h2><p>Add your own books to a private library.</p></div><a className="action-link btn-primary" href={publicHref("/")}>Open library<ArrowRight size={15} /></a></section>
  </main><SiteFooter /></>;
}
