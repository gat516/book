import { useEffect, useState } from "react";
import { getTranslationHealth } from "../api";

interface Props {
  novelId: string;
}

const DISMISSED_KEY = "translation-notice-dismissed";

// Dismissal is per novel and persisted, so a reader who has acknowledged the caveat for a
// book never sees it again for that book. Stored as a set of novel ids rather than a flag
// so dismissing one novel's notice doesn't silence another's.
function dismissedNovels(): string[] {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return []; // private window, blocked storage — just show the notice
  }
}

function rememberDismissal(novelId: string) {
  try {
    const next = Array.from(new Set([...dismissedNovels(), novelId]));
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(next));
  } catch {
    /* dismissal simply won't persist; not worth failing over */
  }
}

// A quiet, one-line caveat shown only when the model is demonstrably naming things
// inconsistently — measured, not guessed (see reader-api's TranslationHealth).
//
// Deliberately restrained: no modal, no colour alarm, no repetition. A warning a reader
// learns to ignore is worse than none, so it appears only past a conservative threshold,
// sits inline above the text, and can be dismissed permanently per novel. Checked once
// when the novel opens rather than polled — terminology stability changes over chapters,
// not seconds.
export function TranslationNotice({ novelId }: Props) {
  const [reason, setReason] = useState<string | null>(null);

  useEffect(() => {
    if (dismissedNovels().includes(novelId)) return;
    let cancelled = false;
    getTranslationHealth(novelId)
      .then((health) => {
        if (!cancelled && health.warn) setReason(health.reason ?? null);
      })
      .catch(() => {
        /* the notice is advisory; never surface its own failure to the reader */
      });
    return () => {
      cancelled = true;
    };
  }, [novelId]);

  if (reason === null) return null;

  return (
    <p className="translation-notice">
      <span>Heads up: {reason}. Names may vary between chapters.</span>
      <button
        onClick={() => {
          rememberDismissal(novelId);
          setReason(null);
        }}
        aria-label="Dismiss translation quality notice"
      >
        ×
      </button>
    </p>
  );
}
