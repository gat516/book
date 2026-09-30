import { useState } from "react";
import { ArrowRight, BookOpen, Check, KeyRound, Library } from "lucide-react";
import { hostedSession } from "../session";
import { Button, Fade } from "./animate-ui/motion";

export function FirstBookGuide({ onCreate, onSettings, onOpen, hasKey = false, hasBook = false }: {
  onCreate: () => void; onSettings?: () => void; onOpen?: () => void; hasKey?: boolean; hasBook?: boolean;
}) {
  const needsKey = hostedSession() && !hasKey;
  const [selected, setSelected] = useState<number | null>(null);
  const step = selected ?? (needsKey ? 0 : hasBook ? 2 : 1);
  const steps = [
    { title: "Connect a provider", text: hostedSession() ? "Add your API key. Your provider bills you directly for usage." : "Use the server provider or add your own API key.", action: hasKey ? "Manage provider keys" : "Connect your provider", icon: KeyRound, click: onSettings ?? onCreate },
    { title: "Add a book", text: "Choose a title, languages, and provider.", action: hasBook ? "Add another book" : "Create your first book", icon: BookOpen, click: onCreate },
    { title: "Add a chapter", text: "Paste chapter text or import from a supported website.", action: hasBook ? "Open your book" : "Create your first book", icon: Library, click: hasBook && onOpen ? onOpen : onCreate },
  ];
  const current = steps[step];
  return <section className="first-book-guide" aria-labelledby="first-book-heading">
    <div className="guide-heading"><div><h2 id="first-book-heading">Getting started</h2></div><span className="guide-step-count">{step + 1} / 3</span></div>
    <div className="guide-layout"><div className="guide-steps" role="group" aria-label="Getting started steps">
      {["Provider", "Book", "First chapter"].map((label, index) => { const done = index === 0 ? hasKey : index === 1 ? hasBook : false; const Icon = steps[index].icon; return <button key={label} aria-pressed={step === index} onClick={() => setSelected(index)}><span className="guide-step-icon">{done ? <Check size={18} /> : <Icon size={18} />}</span><span>{label}<small>{done ? "Ready" : `Step ${index + 1}`}</small></span><ArrowRight size={16} /></button>; })}
    </div><Fade key={step} rise={6} className="guide-detail"><h3>{current.title}</h3><p>{current.text}</p><Button className="btn-primary" onClick={current.click}>{current.action}<ArrowRight size={16} /></Button></Fade></div>
    <div className="guide-demo"><a href="/demo">Read a sample <ArrowRight size={15} /></a></div>
  </section>;
}
