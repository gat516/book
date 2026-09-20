import { useState } from "react";
import { autoUpdate, flip, FloatingFocusManager, FloatingPortal, offset, safePolygon, shift, useClick, useDismiss, useFloating, useHover, useInteractions, useRole } from "@floating-ui/react";
import type { TermRenderingView, WikiPageSummary } from "../types";
import { HoverCard } from "./HoverCard";

interface Props {
  novelId: string; mention: string; rendering?: TermRenderingView; at: number; wikiAt?: number;
  wikiPage?: WikiPageSummary; wikiLoading?: boolean; wikiError?: string | null;
  hoverEnabled: boolean; open: boolean; onOpenChange: (open: boolean) => void;
  onRenderingChanged: (rendering: TermRenderingView) => void;
  onOpenWiki?: (subject: string) => void;
}

export function MentionPopover({ mention, rendering, hoverEnabled, open, onOpenChange, ...card }: Props) {
  const [editing, setEditing] = useState(false);
  const { refs, floatingStyles, context } = useFloating({
    open, onOpenChange(next) { if (!next) setEditing(false); onOpenChange(next); },
    placement: "bottom-start", strategy: "fixed", whileElementsMounted: autoUpdate,
    middleware: [offset(8), flip({ padding: 12 }), shift({ padding: 12 })],
  });
  const hover = useHover(context, { enabled: hoverEnabled && !editing, mouseOnly: true,
    delay: { open: 180, close: 180 }, move: false, handleClose: safePolygon() });
  const click = useClick(context);
  const dismiss = useDismiss(context);
  const role = useRole(context, { role: "dialog" });
  const { getReferenceProps, getFloatingProps } = useInteractions([hover, click, dismiss, role]);
  return <><button ref={refs.setReference} type="button"
    className={`mention mention-button mention-unlinked${rendering?.status === "locked" ? " mention-confirmed" : ""}`}
    {...getReferenceProps({ "aria-label": `Inspect ${mention}` })}>{mention}</button>
    {open && <FloatingPortal><FloatingFocusManager context={context} modal={false} initialFocus={-1} returnFocus>
      <div ref={refs.setFloating} style={floatingStyles} className="mention-popover"
        {...getFloatingProps({ "aria-label": `${mention}: spelling and story wiki`, onFocusCapture: () => setEditing(true) })}>
        <HoverCard {...card} mention={mention} rendering={rendering} onClose={() => { setEditing(false); onOpenChange(false); }} />
      </div>
    </FloatingFocusManager></FloatingPortal>}
  </>;
}
