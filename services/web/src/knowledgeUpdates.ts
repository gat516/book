import { useEffect, useState } from "react";

const eventName = "book-knowledge-updated";
export function notifyKnowledgeUpdated(novelId: string) {
  window.dispatchEvent(new CustomEvent(eventName, { detail: novelId }));
}

// Invalidate all open knowledge views together when a generation or publication changes.
export function useKnowledgeRevision(novelId: string) {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const update = (event: Event) => {
      if ((event as CustomEvent<string>).detail === novelId) setRevision(value => value + 1);
    };
    window.addEventListener(eventName, update);
    return () => window.removeEventListener(eventName, update);
  }, [novelId]);
  return revision;
}
