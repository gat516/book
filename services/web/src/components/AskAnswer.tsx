import { useId, useState } from "react";
import { FloatingFocusManager, FloatingOverlay, FloatingPortal, useDismiss, useFloating, useInteractions, useRole } from "@floating-ui/react";
import { Maximize2, X } from "lucide-react";
import type { AskResponse } from "../types";
import { AnswerMarkdown } from "./AnswerMarkdown";

export function AskAnswer({ response, question }: { response: AskResponse; question: string }) {
  const [expanded, setExpanded] = useState(false);
  const titleId = useId();
  const { refs, context } = useFloating({ open: expanded, onOpenChange: setExpanded });
  const { getFloatingProps } = useInteractions([useDismiss(context), useRole(context, { role: "dialog" })]);
  const chapters = [...new Set(response.retrieved_sources.filter(source => source.chapter <= response.at)
    .map(source => source.chapter))].sort((a, b) => a - b);
  const content = <>
    <div className="ask-answer-scroll" role="region" aria-label="AI answer" tabIndex={0}>
      <p className="ask-answer-question">{question}</p>
      <AnswerMarkdown answer={response.answer} sources={response.retrieved_sources} at={response.at} />
    </div>
    {chapters.length > 0 && <p className="ask-answer-sources">Sources: {chapters.length === 1 ? "chapter" : "chapters"} {chapters.join(", ")}</p>}
  </>;
  return <>
    <section className="ask-box-answer" aria-label="Answer">
      <header className="ask-answer-header"><strong>Answer</strong><button ref={refs.setReference} type="button" className="text-button" onClick={() => setExpanded(true)} aria-label="Expand answer"><Maximize2 size={13} />Expand</button></header>
      {content}
    </section>
    {expanded && <FloatingPortal><FloatingOverlay lockScroll className="ask-answer-overlay">
      <FloatingFocusManager context={context} returnFocus>
        <section ref={refs.setFloating} className="ask-answer-dialog" {...getFloatingProps({ "aria-labelledby": titleId })}>
          <header className="ask-answer-header"><div><h2 id={titleId}>Ask the story</h2><p>Knowledge through chapter {response.at}</p></div><button type="button" className="icon-button" aria-label="Close expanded answer" onClick={() => setExpanded(false)}><X size={20} /></button></header>
          {content}
        </section>
      </FloatingFocusManager>
    </FloatingOverlay></FloatingPortal>}
  </>;
}
