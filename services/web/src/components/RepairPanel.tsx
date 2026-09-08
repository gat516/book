import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelRepair,
  deleteGraph,
  getProviderConfig,
  getRepairStatus,
  listOllamaModels,
  requestRepair,
  retryRepairNow,
} from "../api";
import { DEFAULT_MODEL, MODEL_OPTIONS, PROVIDER_LABELS } from "../providers";
import { usePolling } from "../usePolling";
import type {
  RepairStatus,
  ProviderName,
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
 * Knowledge repair: what is being withheld, and what to do about it.
 *
 * Ungated by choice on this deployment. The extraction list carries source quotes from
 * chapters ahead of the reader, and the controls can quarantine a book's knowledge, so
 * this suits a single-operator install. If it ever serves readers who are not the
 * operator, the gate belongs on READING PROGRESS — show a chapter's names once that
 * chapter has been read — rather than on an admin credential, which answers a different
 * question than the one that matters.
 *
 * Nothing here decides anything about quality. Starting a rebuild, reviewing it and
 * activating it are three separate, explicit actions, and the thresholds that gate the
 * last one live in Python (graph_rebuild.qualified).
 */
export function RepairPanel({ novelId, openSignal = 0 }: Props) {
  const container = useRef<HTMLDetailsElement>(null);
  const [status, setStatus] = useState<RepairStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Per track: the graph and event extractors are separately reviewable and routinely
  // want different models, so one shared input was wrong.
  const [model, setModel] = useState<Record<RepairTrackName, string>>({ graph: "", events: "" });
  const [provider, setProvider] = useState("ollama");
  const [graphProvider, setGraphProvider] = useState<ProviderName>("ollama");
  // KnowledgeEngine refuses anything but a loopback Ollama model, independent of the
  // book's own translate/extract provider (which can be Gemini, DeepSeek or Anthropic).
  // Deriving the graph model from provider config used to leave it permanently blank --
  // and the rebuild button permanently disabled -- for every book not itself configured
  // for Ollama. List what is actually installed locally instead, same as the chapter
  // workspace's one-time build does.
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  // The book's configured extractor is only ever a *guess* for the graph track, so it is
  // held apart from `model` until the installed catalog can confirm it. Seeding `model`
  // directly let a config naming an uninstalled model win over what is actually on the
  // host, and the rebuild then failed server-side with `model_not_installed`.
  const [graphSuggestion, setGraphSuggestion] = useState("");
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

  useEffect(() => {
    void listOllamaModels(novelId, "graph").then(setOllamaModels).catch(() => setOllamaModels([]));
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
        if (config) {
          setGraphProvider(config.provider);
          setGraphSuggestion(config.extract_model || config.model || DEFAULT_MODEL[config.provider] || "");
        }
        if (config?.extract_model && (config.provider === "ollama" || config.provider === "gemini")) {
          setProvider((current) => (current === "ollama" ? config.provider : current));
          setModel((current) => ({ ...current, events: current.events || config.extract_model || "" }));
        }
      })
      .catch(() => undefined);
  }, [novelId]);

  // Seeding the graph model needs BOTH fetches above, which resolve in either order, so
  // it cannot live in either one: keyed on [novelId] alone, whichever lost the race would
  // read the other's state as still-empty and never re-run. Depending on both results
  // instead means this settles once they have arrived.
  //
  // A suggestion that is not installed is dropped rather than substituted. Picking some
  // other entry would be a guess at which local model is a graph extractor -- the catalog
  // also lists embedding models -- and silently rebuilding under a model the operator did
  // not choose is the failure this panel exists to prevent. Blank leaves the button
  // disabled and the datalist open, which asks rather than assumes.
  useEffect(() => {
    if (!graphSuggestion || ollamaModels.length === 0) return;
    if (!ollamaModels.includes(graphSuggestion)) return;
    setModel((current) => (current.graph ? current : { ...current, graph: graphSuggestion }));
  }, [graphSuggestion, ollamaModels]);

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

  async function removeGraph() {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await deleteGraph(novelId);
      const suffix = result.revisions_deleted === 1 ? "" : "s";
      setNotice(
        `Graph deleted (${result.revisions_deleted} revision${suffix}). Chapters, translations, glossary, and reading progress were preserved.`,
      );
      setConfirming(null);
      setReviewing(null);
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
            {track.blocked.retry_eligible_at
              ? ` The worker will retry automatically at ${new Date(
                  track.blocked.retry_eligible_at,
                ).toLocaleTimeString()}.`
              : " This will not clear on its own; fix the cause, then retry."}
            {" "}
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void act(name, "retry", {}, replacement?.revision_id)
              }
            >
              Retry now
            </button>
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

        {(
          <div className="repair-actions">
            {name === "graph" ? (
              <div className="knowledge-gate-action">
                <label className="novel-create-form-hint">Provider{" "}
                  <select value={graphProvider} onChange={(e) => {
                    const next=e.target.value as ProviderName;
                    setGraphProvider(next);
                    setModel((current) => ({...current,graph: next === "ollama" ? "" : DEFAULT_MODEL[next]}));
                  }}>
                    {(Object.keys(PROVIDER_LABELS) as ProviderName[]).map((value) =>
                      <option key={value} value={value}>{PROVIDER_LABELS[value]}</option>)}
                  </select>
                </label>
                <label className="novel-create-form-hint">Model{" "}
                  <input list={`repair-graph-models-${novelId}`} value={model.graph}
                    onChange={(e) => setModel((current) => ({...current,graph:e.target.value}))}
                    placeholder="exact model id" />
                  <datalist id={`repair-graph-models-${novelId}`}>
                    {(graphProvider === "ollama" ? ollamaModels : MODEL_OPTIONS[graphProvider].map((option) => option.id))
                      .map((value) => <option key={value} value={value} />)}
                  </datalist>
                </label>
                {graphProvider !== "ollama" && <span className="novel-create-form-hint">
                  Two model calls for facts in an ordinary chapter: extract, then verify and render.
                </span>}
              </div>
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
                        : { model: model[name], provider: graphProvider },
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

      {status && (
        <>
          {renderTrack("graph", status.graph, "Facts and names")}

          <div className="repair-actions">
            <button
              type="button"
              className="novel-picker-delete"
              disabled={
                busy ||
                (!status.graph.active_revision &&
                  !status.graph.replacement &&
                  status.graph.superseded === 0)
              }
              onClick={() =>
                confirming === "delete-graph"
                  ? void removeGraph()
                  : setConfirming("delete-graph")
              }
            >
              {confirming === "delete-graph"
                ? "Confirm — delete graph permanently"
                : "Delete graph"}
            </button>
            {confirming === "delete-graph" && (
              <>
                <p className="novel-create-form-hint">
                  This permanently deletes every facts-and-names revision for this book,
                  including active, staged, and archived attempts. Chapters, translations,
                  glossary history, and reading progress remain. The graph cannot be recovered.
                </p>
                <button type="button" disabled={busy} onClick={() => setConfirming(null)}>
                  Cancel
                </button>
              </>
            )}
          </div>

          {renderTrack("events", status.events, "Chapter events")}

          {status.requests.length > 0 && (
            <details className="repair-requests">
              <summary>Recent repair actions</summary>
              <ul>
                {status.requests.map((request) => (
                  <li key={request.id}>
                    {request.action} · {request.track} · {request.state} · asked by{" "}
                    {request.requested_by} at{" "}
                    {new Date(request.created_at).toLocaleString()}
                    {request.detail && (
                      <>
                        {" — "}
                        {request.detail}
                        {request.retry_at
                          ? ` Retrying automatically at ${new Date(request.retry_at).toLocaleTimeString()}.`
                          : ""}
                      </>
                    )}
                    {request.state === "pending" && (
                      <>
                        {" "}
                        {request.retry_at && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void retryRepairNow(novelId, request.id).then(load).catch((err) =>
                                setError(String(err)),
                              )
                            }
                          >
                            Retry now
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void cancelRepair(novelId, request.id).then(load).catch((err) =>
                              setError(String(err)),
                            )
                          }
                        >
                          Discard
                        </button>
                      </>
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
