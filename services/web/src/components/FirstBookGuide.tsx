import { useState } from "react";
import { ArrowRight, BookOpen, Check, KeyRound, Sparkles } from "lucide-react";
import { hostedSession } from "../session";
import { Button, Fade } from "./animate-ui/motion";

export function FirstBookGuide({ onCreate, onSettings, onOpen, hasKey = false, hasBook = false }: {
  onCreate: () => void; onSettings?: () => void; onOpen?: () => void; hasKey?: boolean; hasBook?: boolean;
}) {
  const needsKey = hostedSession() && !hasKey;
  const [selected, setSelected] = useState<number | null>(null);
  const step = selected ?? (needsKey ? 0 : hasBook ? 2 : 1);
  const steps = [
    { title: "Bring the AI. We’ll bring the bookshelf.", text: hostedSession() ? "Connect a provider in Account settings to translate and explore your books. Your keys are encrypted; your provider bills you directly for usage." : "Your local books can use the configured server provider. You can also add a provider key in Account settings.", action: hasKey ? "Manage provider keys" : "Connect your provider", icon: KeyRound, click: onSettings ?? onCreate },
    { title: "Give your next story a home.", text: "Add a title, choose the source and reading languages, and select your AI provider. You can fine-tune the model later.", action: hasBook ? "Add another book" : "Create your first book", icon: BookOpen, click: onCreate },
    { title: "One chapter opens a whole world.", text: "Open your book and add a chapter by pasting text or importing from a supported website. As you read, highlighted names lead to the story’s growing wiki.", action: hasBook ? "Open your book" : "Create your first book", icon: Sparkles, click: hasBook && onOpen ? onOpen : onCreate },
  ];
  const current = steps[step];
  return <section className="first-book-guide" aria-labelledby="first-book-heading">
    <div className="guide-heading"><div><p className="eyebrow">A PLACE TO BEGIN</p><h2 id="first-book-heading">Your next world is waiting.</h2></div><span className="guide-step-count">{step + 1} / 3</span></div>
    <div className="guide-layout"><div className="guide-steps" role="group" aria-label="Getting started steps">
      {["Connect your AI", "Create a book", "Read & discover"].map((label, index) => { const done = index === 0 ? hasKey : index === 1 ? hasBook : false; const Icon = steps[index].icon; return <button key={label} aria-pressed={step === index} onClick={() => setSelected(index)}><span className="guide-step-icon">{done ? <Check size={18} /> : <Icon size={18} />}</span><span>{label}<small>{done ? "Ready to go" : `Step ${index + 1}`}</small></span><ArrowRight size={16} /></button>; })}
    </div><Fade key={step} className="guide-detail"><h3>{current.title}</h3><p>{current.text}</p><Button className="btn-primary" onClick={current.click}>{current.action}<ArrowRight size={16} /></Button></Fade></div>
    <div className="guide-demo"><span>Want a look around first?</span><a href="/demo">Explore the sample story <ArrowRight size={15} /></a></div>
  </section>;
}
