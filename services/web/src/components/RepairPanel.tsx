import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelRepair,
  getProviderConfig,
  getRepairStatus,
  listOllamaModels,
  requestRepair,
} from "../api";
import { defaultGraphExtractModel } from "../providers";
import { clearOperatorToken, operatorToken, setOperatorToken } from "../operator";
import { usePolling } from "../usePolling";
import type {
  RepairStatus,
  RepairTrack,
  RepairTrackName,
} from "../types";
import { RepairReview } from "./RepairReview";

interface Props {
  novelId: string;
  // Increments when something elsewhere (the reader's "facts are withheld" notice) wants
  // this panel opened. A counter rather than a boolean so repeated clicks re-open it.
  openSignal?: number;
}

const BUSY_STATES = new Set(["rebuilding", "awaiting_review"]);

function pillClass(state: string): string {
  if (state === "ready") return "repair-pill repair-pill-ok";
  if (state === "failed" || state === "quarantined") return "repair-pill repair-pill-bad";
  return "repair-pill repair-pill-warn";
}

function stateLabel(state: string): string {
  switch (state) {
    case "ready":
      return "Available";
    case "quarantined":
      return "Withheld";
    case "rebuilding":
      return "Rebuilding";
    case "awaiting_review":
      return "Awaiting review";
    case "failed":
      return "Rebuild stopped";
    default:
      return "Not built";
  }
}

/**
 * Knowledge repair: readers see safe status; authenticated operators see controls and
 * whole-book review material that can include future-chapter quotes (spec §0.3).
 *
 * Nothing here decides anything about quality. Starting a rebuild, reviewing it and
 * activating it are three separate, explicit actions, and the thresholds that gate the
 * last one live in Python (graph_rebuild.qualified).
 */
export function RepairPanel({ novelId, openSignal = 0 }: Props) {
  const container = useRef<HTMLDetailsElement>(null);
  const [status, setStatus] = useState<RepairStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState(operatorToken());
  // Per track: the graph and event extractors are separately reviewable and routinely
  // want different models, so one shared input was wrong.
  const [model, setModel] = useState<Record<RepairTrackName, string>>({ graph: "", events: "" });
  const [provider, setProvider] = useState("ollama");
  // KnowledgeEngine refuses anything but a loopback Ollama model, independent of the
  // book's own translate/extract provider (which can be Gemini, DeepSeek or Anthropic).
  // Deriving the graph model from provider config used to leave it permanently blank --
  // and the rebuild button permanently disabled -- for every book not itself configured
  // for Ollama. List what is actually installed locally instead, same as the chapter
  // workspace's one-time build does.
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  const [rollbackTo, setRollbackTo] = useState<Record<RepairTrackName, string>>({ graph: "", events: "" });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<RepairTrackName | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await getRepairStatus(novelId);
      setStatus(next);
      setError(null);
      if (!next.operator && operatorToken()) {
        clearOperatorToken();
        setToken("");
        setNotice("That operator token was not accepted.");
      }
    } catch (err) {
      setError(String(err));
    }
  }, [novelId]);

  useEffect(() => {
    setReviewing(null);
    setConfirming(null);
    setNotice(null);
    void load();
  }, [load]);

  const active =
    status !== null &&
    (BUSY_STATES.has(status.graph.state) ||
      BUSY_STATES.has(status.events.state) ||
      status.requests.some((request) => request.state === "pending" || request.state === "running"));

  // Same busy/idle cadence as PipelineStatus: frequent while something is moving, rare
  // when nothing is. A rebuild takes minutes per chapter on this hardware, so polling
  // faster than this would show the same numbers repeatedly.
  usePolling(load, active ? 8000 : 30000, true);

  useEffect(() => {
    // 0 is the initial value, so the panel is not forced open on first render.
    if (!openSignal || !container.current) return;
    container.current.open = true;
    container.current.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [openSignal]);

  function saveToken() {
    setOperatorToken(token.trim());
    void load();
  }

  useEffect(() => {
    void listOllamaModels(novelId).then(setOllamaModels).catch(() => setOllamaModels([]));
  }, [novelId]);

  useEffect(() => {
    // The events track's model is the book's own configured extraction model, full stop
    // -- there is no separate choice here to seed and then forget to write back to. The
    // graph track cannot use that provider at all (KnowledgeEngine refuses anything but a
    // loopback Ollama), so its model is picked from what is actually installed locally
    // (the ollamaModels effect above), not from provider config. A book's provider only
    // supplies a starting guess when it happens to already be Ollama.
    void getProviderConfig(novelId)
      .then((config) => {
        const graphDefault = defaultGraphExtractModel(config);
        if (graphDefault) {
          setModel((current) => ({ ...current, graph: current.graph || graphDefault }));
        }
        if (config?.extract_model && (config.provider === "ollama" || config.provider === "gemini")) {
          setProvider((current) => (current === "ollama" ? config.provider : current));
          setModel((current) => ({ ...current, events: current.events || config.extract_model || "" }));
        }
      })
      .catch(() => undefined);
  }, [novelId]);

  async function act(
    track: RepairTrackName,
    action: string,
    params?: unknown,
    revisionId?: string,
  ) {
    setBusy(true);
    setNotice(null);
    try {
      await requestRepair(novelId, {
        track,
        action,
        revision_id: revisionId,
        params,
      });
      setNotice(
        "Recorded. Repair runs when the worker is not busy with chapters someone is waiting to read, so this may not start immediately.",
      );
      setConfirming(null);
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  function renderTrack(name: RepairTrackName, track: RepairTrack, heading: string) {
    const replacement = track.replacement;
    // switch() refuses anything but an archived revision, so rollback offers exactly what
    // the server says is archived. The active revision is never in this list.
    const target = rollbackTo[name] || track.rollback_targets[0]?.revision_id || "";
    const chosen = track.rollback_targets.find((option) => option.revision_id === target);
    return (
      <div className="repair-track" key={name}>
        <h4>
          {heading} <span className={pillClass(track.state)}>{stateLabel(track.state)}</span>
        </h4>
        <p className="repair-reason">{track.reason}</p>

        {track.blocked && (
          <p className="chapter-list-error" role="alert">
            Stalled: {track.blocked.detail}
            {track.blocked.since &&
              ` (since ${new Date(track.blocked.since).toLocaleTimeString()})`}
          </p>
        )}

        {track.state === "rebuilding" && !track.blocked && (
          <p className="repair-live" role="status">
            {track.published.calls} model call
            {track.published.calls === 1 ? "" : "s"} completed
            {track.published.claims > 0 &&
              `, ${track.published.claims} claim${track.published.claims === 1 ? "" : "s"} published`}
            . A chapter needs many calls before any of it is published, so the call count
            is what moves while a chapter is in flight.
          </p>
        )}

        {track.state !== "ready" && track.withheld_claims > 0 && (
          <p className="novel-create-form-hint">
            {track.withheld_claims} stored {name === "graph" ? "facts" : "events"} are
            hidden from every reader and from Ask AI while this lasts. Saved translations
            are untouched.
          </p>
        )}

        {replacement && (
          <dl className="repair-replacement">
            <div>
              <dt>Replacement</dt>
              <dd>
                {replacement.model ?? "unknown model"}
                {replacement.prompt_version ? ` · ${replacement.prompt_version}` : ""}
              </dd>
            </div>
            <div>
              <dt>Chapters</dt>
              <dd>
                {track.chapters.done} done
                {track.chapters.failed > 0 && `, ${track.chapters.failed} failed`}
                {track.chapters.running > 0 && `, ${track.chapters.running} running`} of{" "}
                {track.chapters.total}
              </dd>
            </div>
            {replacement.activation_eligible !== null && (
              <div>
                <dt>Report</dt>
                <dd>
                  {replacement.activation_eligible
                    ? "meets the activation thresholds"
                    : "does not yet meet the activation thresholds"}
                </dd>
              </div>
            )}
            {track.superseded > 0 && (
              <div>
                <dt>Earlier attempts</dt>
                <dd>{track.superseded} superseded</dd>
              </div>
            )}
          </dl>
        )}

        {track.failures.length > 0 && (
          <details className="repair-failures">
            <summary>
              {track.failures.length} failed chapter
              {track.failures.length === 1 ? "" : "s"}
              {!track.retryable && " · no retries left"}
            </summary>
            <ul>
              {track.failures.map((failure) => (
                <li key={`${failure.chapter_index}-${failure.occurred_at}`}>
                  Chapter {failure.chapter_index} · attempt {failure.attempts} ·{" "}
                  {failure.detail}
                  {failure.retry_at
                    ? ` Retrying at ${new Date(failure.retry_at).toLocaleTimeString()}.`
                    : " No further retries are scheduled."}
                </li>
              ))}
            </ul>
            {!track.retryable && (
              <p className="novel-create-form-hint">
                Retrying will not help: every failure has used its attempts. Change the
                model or its runtime settings, then start a fresh rebuild — a new rebuild
                pins the model it starts with.
              </p>
            )}
          </details>
        )}

        {status?.operator && (
          <div className="repair-actions">
            {name === "graph" ? (
              // The graph track cannot use the book's configured provider at all
              // (KnowledgeEngine refuses anything but a loopback Ollama) -- deriving its
              // model from provider config left this permanently blank, and the button
              // permanently disabled, for every book not itself set to Ollama. Pick from
              // what is actually installed locally instead.
              <label className="novel-create-form-hint">
                Model{" "}
                <select
                  value={model.graph}
                  onChange={(e) => setModel((current) => ({ ...current, graph: e.target.value }))}
                  disabled={ollamaModels.length === 0}
                >
                  <option value="">{ollamaModels.length === 0 ? "No local Ollama models found" : "Choose a model…"}</option>
                  {ollamaModels.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                {ollamaModels.length === 0 && (
                  <> — the entity graph always runs on a local Ollama model, regardless of
                  this book's configured provider.</>
                )}
              </label>
            ) : (
              // The events track, unlike graph, can use the book's own configured
              // provider (event_rebuild.EXTRACTION_PROVIDERS includes it) -- so showing
              // what a fresh rebuild would use, rather than a second choice made here
              // that could quietly disagree with it, is the right default.
              <p className="novel-create-form-hint">
                {model[name]
                  ? <>Uses <strong>{model[name]}</strong>{provider !== "ollama" && ` (${provider})`}, the book's configured extraction model.</>
                  : "No extraction model is configured for this book yet."}
                {" "}<a href="#provider-config">Change it in Book settings</a>.
              </p>
            )}
            <button
              type="button"
              disabled={busy || !model[name]}
              onClick={() =>
                confirming === `prepare-${name}`
                  ? void act(
                      name,
                      "prepare",
                      name === "events"
                        ? { model: model[name], provider }
                        : { model: model[name] },
                    )
                  : setConfirming(`prepare-${name}`)
              }
            >
              {confirming === `prepare-${name}`
                ? `Confirm — re-extract all ${track.chapters.total || "?"} chapters`
                : "Start fresh rebuild"}
            </button>
            {replacement?.review_hash && (
              <button type="button" disabled={busy} onClick={() => setReviewing(name)}>
                Review claims
              </button>
            )}
            {replacement?.review_hash ? (
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  confirming === `activate-${name}`
                    ? void act(
                        name,
                        "activate",
                        { review_hash: replacement.review_hash },
                        replacement.revision_id,
                      )
                    : setConfirming(`activate-${name}`)
                }
              >
                {confirming === `activate-${name}` ? "Confirm activate" : "Activate"}
              </button>
            ) : (
              replacement?.reviewed && (
                // record_review stores its metrics and clears `review` in one step, so
                // the activation hash does not exist until the worker freezes a fresh
                // report. A disabled button that says why beats one that vanishes.
                <button type="button" disabled title="Waiting for a fresh report">
                  Activate (report pending)
                </button>
              )
            )}
            {track.rollback_targets.length > 0 && (
              <>
                <label className="repair-rollback">
                  Roll back to
                  <select
                    value={target}
                    onChange={(event) =>
                      setRollbackTo((current) => ({ ...current, [name]: event.target.value }))
                    }
                  >
                    {track.rollback_targets.map((option) => (
                      <option key={option.revision_id} value={option.revision_id}>
                        {new Date(option.created_at).toLocaleString()}
                        {option.trusted ? "" : " (untrusted)"}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  disabled={busy || !target}
                  onClick={() =>
                    confirming === `rollback-${name}`
                      ? void act(name, "rollback", {}, target)
                      : setConfirming(`rollback-${name}`)
                  }
                >
                  {confirming === `rollback-${name}` ? "Confirm roll back" : "Roll back"}
                </button>
              </>
            )}
          </div>
        )}
        {confirming === `prepare-${name}` && (
          <p className="novel-create-form-hint">
            A rebuild is whole-book by construction: identity resolution for a chapter
            draws on the entities every earlier chapter established, so there is no way to
            redo one chapter in isolation. This re-extracts all{" "}
            {track.chapters.total || "?"} chapters from scratch with{" "}
            <strong>{model[name] || "the chosen model"}</strong>, quarantines the current
            {name === "graph" ? " facts" : " events"} until the result is reviewed, and
            supersedes any rebuild already in progress. On local hardware that is hours to
            days, not minutes.
          </p>
        )}

        {confirming === `rollback-${name}` && (
          <p className="novel-create-form-hint">
            {chosen && !chosen.trusted
              ? "That revision is quarantined. Rolling back to it does not restore its facts — they stay withheld. Use this to undo an activation, not to recover from quarantine."
              : "Rolling back replaces the active revision. The one it replaces becomes archived and can be rolled back to in turn."}
          </p>
        )}
      </div>
    );
  }

  if (reviewing && status) {
    return (
      <RepairReview
        novelId={novelId}
        track={reviewing}
        onClose={() => setReviewing(null)}
        onSubmitted={() => {
          setReviewing(null);
          setNotice("Review submitted. The server checks it against the stored bindings.");
          void load();
        }}
      />
    );
  }

  // Nothing to say while everything is fine and the reader is not an operator.
  const quiet =
    status !== null &&
    !openSignal &&
    status.graph.state === "ready" &&
    (status.events.state === "ready" || status.events.state === "unavailable") &&
    status.requests.length === 0;
  if (quiet) return null;

  return (
    <details className="reader-settings repair-panel" ref={container}>
      <summary>Knowledge repair</summary>

      {error && (
        <p className="chapter-list-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="novel-create-form-hint">
          {notice}
        </p>
      )}

      {status && !status.operator && (
        <div className="knowledge-gate-action">
          <label>
            Operator token{" "}
            <input type="password" value={token} onChange={(event) => setToken(event.target.value)} />
          </label>
          <button type="button" disabled={!token.trim()} onClick={saveToken}>Unlock repair controls</button>
          <p className="novel-create-form-hint">Whole-book review can contain future-chapter quotes, so repair controls require separate operator authorization.</p>
        </div>
      )}

      {status && (
        <>
          {renderTrack("graph", status.graph, "Facts and names")}
          {renderTrack("events", status.events, "Chapter events")}

          {status.requests.length > 0 && (
            <details className="repair-requests">
              <summary>Recent repair actions</summary>
              <ul>
                {status.requests.map((request) => (
                  <li key={request.id}>
                    {request.action} · {request.track} · {request.state}
                    {request.category ? ` (${request.category})` : ""} · asked by{" "}
                    {request.requested_by} at{" "}
                    {new Date(request.created_at).toLocaleString()}
                    {status.operator && request.state === "pending" && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void cancelRepair(novelId, request.id).then(load).catch((err) =>
                            setError(String(err)),
                          )
                        }
                      >
                        Cancel
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {status.history.length > 0 && (
            <details className="repair-history">
              <summary>Repair history ({status.history.length})</summary>
              <ul>
                {status.history.map((entry) => (
                  <li key={`${entry.track}-${entry.action}-${entry.created_at}`}>
                    {new Date(entry.created_at).toLocaleString()} · {entry.track} ·{" "}
                    {entry.action}
                  </li>
                ))}
              </ul>
              <p className="novel-create-form-hint">
                Quarantine is always a deliberate decision — nothing quarantines a book on
                its own. A long list here means this book has needed repairing repeatedly.
              </p>
            </details>
          )}
        </>
      )}
    </details>
  );
}
