import { useEffect, useRef } from "react";

/**
 * Run `callback` on an interval, but only while it's worth running.
 *
 * Three independent components here poll the server (chapter list, pipeline status,
 * pending-chapter view). Left naive they each ran their own unconditional setInterval,
 * which meant a page could re-fetch and re-render continuously — including in a
 * backgrounded tab, and including when nothing was changing server-side. On a machine
 * already under memory pressure that was enough to make the UI lag badly.
 *
 * Two guards:
 *  - `enabled`: callers pass false when there is genuinely nothing to watch, so an idle
 *    page settles to zero network traffic instead of a permanent heartbeat.
 *  - page visibility: a hidden tab polls nothing, and refreshes once on return so the
 *    view is current rather than stale-then-slowly-correcting.
 *
 * The callback is held in a ref so changing it doesn't restart the timer — otherwise a
 * closure recreated each render silently resets the interval on every tick.
 */
export function usePolling(callback: () => void, intervalMs: number, enabled: boolean) {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (!enabled) return;

    let timer: ReturnType<typeof setInterval> | null = null;

    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };

    const start = () => {
      if (timer) return;
      timer = setInterval(() => saved.current(), intervalMs);
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        stop();
      } else {
        saved.current(); // catch up immediately, then resume the cadence
        start();
      }
    };

    if (document.visibilityState !== "hidden") start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs, enabled]);
}
