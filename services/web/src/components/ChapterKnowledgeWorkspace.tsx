import { useCallback, useEffect, useState } from "react";
import {
  applyChapterReextract, correctFact, correctGlossaryTerm, deleteGlossaryTerm,
  editFactDisplay, getChapterKnowledge, getChapterKnowledgeActivity, getProviderConfig,
  getRepairStatus, listOllamaModels, removeFact, requestRepair, startChapterReextract,
} from "../api";
import { DEFAULT_MODEL, MODEL_OPTIONS, PROVIDER_LABELS } from "../providers";
import type { ChapterKnowledgeActivity, ChapterKnowledgeResponse, ProviderName, RepairStatus } from "../types";
import { RepairReview } from "./RepairReview";

const terminal = new Set(["published", "rejected", "failed", "awaiting_review"]);

function shortId(id: string) { return id.slice(0, 8); }

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function extractionStatus(data: ChapterKnowledgeResponse, scope: "terms" | "facts") {
  const extracted = scope === "terms" ? data.terms_extracted : data.facts_extracted;
  const run = data.run;
  const relevant = run && (run.scope === "all" || run.scope === scope);
  if (relevant && (run.state === "pending" || run.state === "processing")) return "Extracting…";
  if (relevant && (run.state === "awaiting_review" || run.state === "applying")) return "Review pending";
  if (relevant && run.state === "failed" && !extracted) return "Failed";

  const rebuild = data.graph_extraction;
  if (rebuild?.state === "pending" || rebuild?.state === "processing") return "Extracting in book rebuild…";
  if (rebuild?.state === "failed" && !extracted) return "Book rebuild failed";
  if (rebuild?.state === "done") {
    const count = scope === "terms" ? rebuild.verified_terms : rebuild.verified_claims;
    const noun = scope === "terms" ? (count === 1 ? "term" : "terms") : (count === 1 ? "claim" : "claims");
    return `Extracted — ${count} verified ${noun} awaiting activation`;
  }
  return extracted ? "Extracted" : "Not extracted";
}

function graphBuildActivity(status: RepairStatus | null) {
  if (!status) return null;
  const graph = status.graph;
  // A replacement revision is the work actually consuming the model. If somebody also
  // queued a fresh prepare, leading with that queue row makes a busy worker look idle.
  if (graph.replacement && ["rebuilding", "awaiting_review", "failed", "quarantined"].includes(graph.state)) {
    return {
      at: graph.current?.since ?? graph.replacement.created_at ?? "",
      stage: graph.state === "rebuilding" ? "building"
        : graph.state === "awaiting_review" ? "review"
        : graph.state,
      text: graph.reason,
    };
  }
  const request = status.requests.find(
    (item) => item.track === "graph" && (item.state === "pending" || item.state === "running"),
  );
  if (request) {
    return {
      at: request.created_at,
      stage: request.state === "pending" ? "queued" : "starting",
      text: request.state === "pending"
        ? "Book graph build is queued and waiting for the worker."
        : "Book graph build is being prepared.",
    };
  }
  return null;
}

// The one-time build a never-rebuilt or not-yet-included book needs: KnowledgeEngine
// requires a pinned model and a snapshot neither a legacy revision nor a missing chapter
// has. Adopting a legacy revision instead of building one would have to fabricate the
// graph_evidence rows the literal-evidence check depends on (§0: structural, not
// prompted), so the one-time build is the honest path.
function BuildGraph({ novelId, chapter, label, onDone }: { novelId: string; chapter: number; label: string; onDone: () => Promise<unknown> }) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState<ProviderName>("ollama");
  const [upto, setUpto] = useState(chapter);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void listOllamaModels(novelId, "graph").then(setModels).catch(() => setModels([]));
    void getProviderConfig(novelId)
      .then((config) => {
        if (!config) return;
        setProvider(config.provider);
        setModel((current) => current || config.extract_model || config.model || DEFAULT_MODEL[config.provider]);
      })
      .catch(() => undefined);
  }, [novelId]);
  useEffect(() => setUpto(chapter), [novelId, chapter]);

  async function build() {
    setBusy(true); setError("");
    try {
      await requestRepair(novelId, {
        track: "graph", action: "prepare", params: { provider, model, upto_chapter: upto },
      });
      await onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="knowledge-gate-action">
      <label>Provider{" "}
        <select value={provider} onChange={(e) => {
          const next=e.target.value as ProviderName;
          setProvider(next);
          setModel(next === "ollama" ? "" : DEFAULT_MODEL[next]);
        }}>
          {(Object.keys(PROVIDER_LABELS) as ProviderName[]).map((name) =>
            <option key={name} value={name}>{PROVIDER_LABELS[name]}</option>)}
        </select>
      </label>
      <label>Model{" "}
        <input list={`graph-models-${novelId}`} value={model}
          onChange={(e) => setModel(e.target.value)} placeholder="exact model id" />
        <datalist id={`graph-models-${novelId}`}>
          {(provider === "ollama" ? models : MODEL_OPTIONS[provider].map((option) => option.id))
            .map((name) => <option key={name} value={name} />)}
        </datalist>
      </label>
      {provider !== "ollama" && <p className="novel-create-form-hint">
        Uses the saved {PROVIDER_LABELS[provider]} API key and the two-pass fact strategy.
      </p>}
      <label>Through chapter{" "}
        <input type="number" min={0} step={1} value={upto}
          onChange={(e) => setUpto(Number(e.target.value))} />
      </label>
      <button disabled={busy || !model || !Number.isInteger(upto) || upto < 0}
        onClick={() => void build()}>{busy ? "Queueing…" : `${label} through chapter ${upto}`}</button>
      {error && <p role="alert" className="reader-pane-error">{error}</p>}
    </div>
  );
}

function ExtendGraph({ novelId, chapter, onDone }: { novelId: string; chapter: number; onDone: () => Promise<unknown> }) {
  const [upto, setUpto] = useState(chapter);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setUpto(chapter), [novelId, chapter]);

  async function extend() {
    setBusy(true); setError("");
    try {
      await requestRepair(novelId, {
        track: "graph", action: "extend", params: { upto_chapter: upto },
      });
      await onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return <div className="knowledge-gate-action">
    <label>Through chapter{" "}
      <input type="number" min={0} step={1} value={upto}
        onChange={(e) => setUpto(Number(e.target.value))} />
    </label>
    <button disabled={busy || !Number.isInteger(upto) || upto < 0}
      onClick={() => void extend()}>{busy ? "Queueing…" : `Extend through chapter ${upto}`}</button>
    {error && <p role="alert" className="reader-pane-error">{error}</p>}
  </div>;
}

// A rebuild left unfinished withholds facts until it is finished, activated or
// discarded. `discard` is the escape prepare()'s precautionary quarantine never had: it
// undoes THAT quarantine when nothing since has re-quarantined the same revision,
// restoring the earlier facts; otherwise it says plainly that they stay withheld.
function QuarantineGate({ novelId, chapter, onDone }: { novelId: string; chapter: number; onDone: () => Promise<unknown> }) {
  const [status, setStatus] = useState<RepairStatus | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(
    () => getRepairStatus(novelId).then(setStatus).catch((e) => setError(errorMessage(e))),
    [novelId],
  );
  useEffect(() => { void load(); }, [load]);

  if (!status) return <p className="knowledge-gate">{error || "Loading rebuild status…"}</p>;
  const track = status.graph;
  const replacement = track.replacement;

  async function act(action: string, params?: unknown) {
    setBusy(true); setError("");
    try {
      await requestRepair(novelId, { track: "graph", action, revision_id: replacement?.revision_id, params });
      setConfirming(null);
      await load();
      await onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (reviewing) {
    return <RepairReview novelId={novelId} track="graph" onClose={() => setReviewing(false)}
      onSubmitted={() => { setReviewing(false); void load(); }} />;
  }

  if (!replacement) {
    // The active graph is untrusted but nothing is replacing it — e.g. a rollback landed
    // on a distrusted revision. There is nothing to discard here; the way out is the same
    // one-time build as a book that was never rebuilt.
    return <div className="knowledge-gate">
      <p>Facts are withheld and no rebuild is in progress.</p>
      <BuildGraph novelId={novelId} chapter={chapter} label="Start a rebuild" onDone={async () => { await load(); await onDone(); }} />
      {error && <p role="alert" className="reader-pane-error">{error}</p>}
    </div>;
  }

  return (
    <div className="knowledge-gate-action">
      <p>
        Facts are withheld while a rebuild is unfinished. Started{" "}
        {replacement.created_at ? new Date(replacement.created_at).toLocaleString() : "recently"}, revision{" "}
        {shortId(replacement.revision_id)}, {track.chapters.done}/{track.chapters.total} chapters extracted.
        {" "}It resumes automatically when the worker is not busy with a chapter someone is waiting to read.
      </p>
      {track.blocked && (
        <p role="alert" className="reader-pane-error">
          Stalled: {track.blocked.detail}
          {track.blocked.retry_eligible_at
            ? ` The worker will retry automatically at ${new Date(track.blocked.retry_eligible_at).toLocaleTimeString()}.`
            : " This will not clear on its own; fix the cause, then retry."}
        </p>
      )}
      <div className="knowledge-actions">
        {track.blocked && (
          <button disabled={busy} onClick={() => void act("retry")}>Retry now</button>
        )}
        {replacement.review_hash && <button disabled={busy} onClick={() => setReviewing(true)}>Review</button>}
        {replacement.review_hash && (
          <button disabled={busy} onClick={() =>
            confirming === "activate"
              ? void act("activate", { review_hash: replacement.review_hash })
              : setConfirming("activate")}>
            {confirming === "activate" ? "Confirm activate" : "Activate"}
          </button>
        )}
        <button disabled={busy} onClick={() =>
          confirming === "discard" ? void act("discard") : setConfirming("discard")}>
          {confirming === "discard" ? "Confirm discard" : "Discard rebuild"}
        </button>
      </div>
      {confirming === "discard" && <p className="novel-create-form-hint">
        Discards this unfinished rebuild. If nothing else has quarantined the earlier
        revision since this one started, its facts return; otherwise they stay withheld
        and you can start a fresh rebuild.
      </p>}
      {error && <p role="alert" className="reader-pane-error">{error}</p>}
    </div>
  );
}

// Which of four states this chapter's knowledge is in, and the one action that resolves
// it — not a single banner naming the wrong cause for three of the four. `trusted` alone
// used to stand in for `can_extract`, which reads "writable" for every never-rebuilt book
// (0023's initialize trigger makes the first revision trusted AND legacy); migration 0058
// added the real predicate so this switch has something honest to key on.
function KnowledgeGate({ novelId, chapter, reason, repairStatus, onDone }: { novelId: string; chapter: number; reason: string; repairStatus: RepairStatus | null; onDone: () => Promise<unknown> }) {
  const graphActivity = graphBuildActivity(repairStatus);
  // A replacement existing AT ALL -- healthily rebuilding, blocked, awaiting review,
  // failed, whatever -- is exactly the state QuarantineGate exists for: discard is
  // always a valid action on an unfinished rebuild, not only once something has gone
  // wrong with it. Gating this on `state` (as an earlier version did) routed a perfectly
  // healthy in-progress rebuild to the plain activity-text branch below instead, with no
  // way to discard or retry it short of calling the API directly -- the reader could
  // watch "Rebuilding: 4 of 26..." for hours with no button at all.
  const replacementNeedsAction = !!repairStatus?.graph.replacement;
  if (replacementNeedsAction) {
    return <div className="knowledge-gate"><QuarantineGate novelId={novelId} chapter={chapter} onDone={onDone} /></div>;
  }
  if (graphActivity) {
    return <div className="knowledge-gate"><p>{graphActivity.text}</p></div>;
  }
  if (reason === "never_built") {
    return <div className="knowledge-gate">
      <p>Build a graph from the ready chapters through N. It stops at the first missing
        chapter. After review and activation, you can extend it to later chapters.</p>
      <BuildGraph novelId={novelId} chapter={chapter} label="Build" onDone={onDone} />
    </div>;
  }
  if (reason === "quarantined") {
    return <div className="knowledge-gate"><QuarantineGate novelId={novelId} chapter={chapter} onDone={onDone} /></div>;
  }
  if (reason === "chapter_not_snapshotted") {
    return <div className="knowledge-gate">
      <p>This chapter is beyond the active graph ceiling. Extend the existing graph to
        process the next ready chapters in order.</p>
      <ExtendGraph novelId={novelId} chapter={chapter} onDone={onDone} />
    </div>;
  }
  return null;
}

export function ChapterKnowledgeWorkspace({novelId,chapter,at}:{novelId:string;chapter:number;at:number}) {
  const [data,setData]=useState<ChapterKnowledgeResponse|null>(null);
  const [activity,setActivity]=useState<ChapterKnowledgeActivity[]>([]);
  const [repairStatus,setRepairStatus]=useState<RepairStatus|null>(null);
  const [ollamaStatus,setOllamaStatus]=useState<"checking"|"connected"|"unreachable">("checking");
  const [error,setError]=useState(""); const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState<number|null>(null); const [draft,setDraft]=useState("");
  const [editingTerm,setEditingTerm]=useState<string|null>(null); const [termDraft,setTermDraft]=useState("");
  const [decisions,setDecisions]=useState<Record<string,string>>({});
  const load=useCallback(async()=>{const next=await getChapterKnowledge(novelId,chapter);setData(next);return next},[novelId,chapter]);
  const loadRepair=useCallback(async()=>{const next=await getRepairStatus(novelId);setRepairStatus(next);return next},[novelId]);
  const refresh=useCallback(async()=>{const [next]=await Promise.all([load(),loadRepair()]);return next},[load,loadRepair]);

  useEffect(()=>{setData(null);setActivity([]);setRepairStatus(null);setError("");void refresh().catch(e=>setError(errorMessage(e)))},[refresh]);
  useEffect(()=>{
    const run=data?.run;if(!run)return;let cancelled=false;let timer:number|undefined;
    async function poll(){try{const after=activity.at(-1)?.sequence??0;const response=await getChapterKnowledgeActivity(novelId,chapter,run!.id,after);if(cancelled)return;if(response.activity.length)setActivity(old=>[...old,...response.activity]);const fresh=await load();if(!terminal.has(fresh.run?.state??""))timer=window.setTimeout(poll,2000)}catch{if(!cancelled)timer=window.setTimeout(poll,5000)}}
    void poll();return()=>{cancelled=true;if(timer)clearTimeout(timer)};
    // sequence is deliberately read when each poll runs; restarting on every item duplicates requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[data?.run?.id,data?.run?.state,novelId,chapter,load]);

  const graphActivity = graphBuildActivity(repairStatus);
  const graphMoving = repairStatus?.requests.some(
    (item) => item.track === "graph" && (item.state === "pending" || item.state === "running"),
  ) || repairStatus?.graph.state === "rebuilding";
  useEffect(()=>{
    if(!graphMoving)return;
    const timer=window.setInterval(()=>{void refresh().catch(()=>undefined)},2000);
    return()=>window.clearInterval(timer);
  },[graphMoving,refresh]);
  const watchOllama = !data?.can_extract || !!graphMoving || !!data?.run && !terminal.has(data.run.state);
  useEffect(()=>{
    if(!watchOllama)return;
    let cancelled=false;let timer:number|undefined;
    async function check(){
      try{await listOllamaModels(novelId,"graph");if(!cancelled)setOllamaStatus("connected")}
      catch{if(!cancelled)setOllamaStatus("unreachable")}
      finally{if(!cancelled)timer=window.setTimeout(check,2000)}
    }
    setOllamaStatus("checking");void check();
    return()=>{cancelled=true;if(timer)window.clearTimeout(timer)};
  },[novelId,watchOllama]);

  // A chapter can finish background publication while this panel is open. Read the
  // current graph fence immediately before every write; §0 still rejects a genuinely
  // concurrent change, but a merely old browser snapshot no longer causes a false stale edit.
  async function mutate(work:(current:ChapterKnowledgeResponse)=>Promise<unknown>){setBusy(true);setError("");try{const current=await load();await work(current);await load()}catch(e){try{await load()}catch{/* retain the mutation error */}setError(errorMessage(e))}finally{setBusy(false)}}
  if(!data)return <section className="chapter-knowledge"><h2>Chapter knowledge</h2><p>{error||"Loading knowledge…"}</p></section>;
  const preview=data.run?.preview?.items??[];
  const writable=data.can_extract;
  return <section className="chapter-knowledge" aria-labelledby="chapter-knowledge-heading">
    <header><div><h2 id="chapter-knowledge-heading">Chapter knowledge</h2><p>Extract terms finds this chapter's named terms; extract facts finds the claims it supports. Proposed items stay out of cards and Ask AI until published.</p></div>
      <span className="knowledge-actions"><button disabled={!writable||busy||!!data.run&&!terminal.has(data.run.state)} onClick={()=>void mutate(()=>startChapterReextract(novelId,chapter,"terms"))}>Extract terms</button><button disabled={!writable||busy||!!data.run&&!terminal.has(data.run.state)} onClick={()=>void mutate(()=>startChapterReextract(novelId,chapter,"facts"))}>Extract facts</button></span></header>
    <dl className="repair-review-progress" aria-label="Extraction status">
      <div><dt>Terms</dt><dd>{extractionStatus(data,"terms")}</dd></div>
      <div><dt>Facts</dt><dd>{extractionStatus(data,"facts")}</dd></div>
      {watchOllama&&<div><dt>Local model</dt><dd>{ollamaStatus==="connected"?"Connected":ollamaStatus==="checking"?"Checking…":"Unreachable"}</dd></div>}
    </dl>
    {watchOllama&&ollamaStatus==="unreachable"&&<p role="alert" className="reader-pane-error">Local Ollama is unreachable. Graph extraction cannot continue until the server and its connection are available.</p>}
    {error&&<p role="alert" className="reader-pane-error">{error}</p>}
    {!writable&&<KnowledgeGate novelId={novelId} chapter={chapter} reason={data.blocked_reason} repairStatus={repairStatus} onDone={refresh} />}
    <details open><summary>Activity {data.run?`— ${data.run.scope} ${data.run.state.replace("_"," ")}`:graphActivity&&"— book graph"}</summary>
      {!graphActivity&&activity.length===0?<p>No active extraction activity.</p>:<ol className="knowledge-activity">
        {graphActivity&&<li key="graph-build">{graphActivity.at&&<time>{new Date(graphActivity.at).toLocaleTimeString()}</time>} <span className="knowledge-badge">{graphActivity.stage}</span> {graphActivity.text}</li>}
        {activity.map(a=><li key={a.sequence}><time>{new Date(a.created_at).toLocaleTimeString()}</time> <span className={`knowledge-badge phase-${a.phase}`}>{a.phase}</span> {a.item_kind} {a.phase==="proposed"&&<em> — unverified</em>}</li>)}
      </ol>}
      {data.run?.state==="awaiting_review"&&<div className="knowledge-review"><h3>{data.run.scope==="terms"?"Term extraction review":"Fact extraction review"}</h3><p>{data.run.scope==="terms"?"Publish verified term occurrences. This does not extract or change facts.":"Nothing below changes the graph unless you explicitly select it. Missing model claims default to retain."}</p>
        {preview.map(item=><label key={item.item_key}><span>{item.item_kind}: {item.classification.replace("_"," ")}</span>{item.item_kind==="term"?<small>Verified terms will be published; the term itself remains editable below.</small>:<select value={decisions[item.item_key]??"retain"} onChange={e=>setDecisions(old=>({...old,[item.item_key]:e.target.value}))}><option value="retain">Retain current knowledge</option>{item.classification==="new"&&<option value="approve">Publish new item</option>}{item.classification==="display_update"&&<option value="update_display">Update English display</option>}{item.classification==="possible_replacement"&&<option value="replace">Publish correction</option>}{item.classification==="missing"&&<option value="remove">Publish retraction</option>}</select>}</label>)}
        <button disabled={busy} onClick={()=>void mutate(current=>applyChapterReextract(novelId,chapter,current.run?.id||data.run!.id,{revision_id:current.revision_id,version:current.version,decisions}))}>{data.run.scope==="terms"?"Publish verified terms":Object.values(decisions).some(v=>v!=="retain")?"Apply selected changes":"Finish review — retain everything"}</button></div>}
    </details>
    <details open><summary>Facts ({data.facts.length} published)</summary>
      {data.facts.length===0?<p>{data.graph_extraction?.state==="done"
        ? `${data.graph_extraction.published_fact_rows} fact rows (${data.graph_extraction.verified_claims} verified claims total) are in the rebuild awaiting review and activation.`
        : "No published facts originated here."}</p>:<ul className="chapter-knowledge-list">{data.facts.map(f=><li key={f.id} className={f.status!=="active"?"knowledge-history":""}>
        <div><strong>{f.entity_canonical}</strong> — {f.attribute}: {f.value} {f.kind!=="assertion"&&<span className="knowledge-badge">{f.kind}</span>} {f.status!=="active"&&<span className="knowledge-badge">{f.status}</span>}</div>
        <small>Source: {f.value_source}{f.evidence?.quote&&<> · Evidence: “{f.evidence.quote}”</>}</small>
        {f.status==="active"&&<div className="knowledge-actions">{editing===f.id?<><input aria-label="English display value" value={draft} onChange={e=>setDraft(e.target.value)}/><button disabled={!writable||busy||!draft.trim()} onClick={()=>void mutate(async current=>{await editFactDisplay(novelId,f.id,{revision_id:current.revision_id,version:current.version,value_en:draft.trim()});setEditing(null)})}>Save display</button><button onClick={()=>setEditing(null)}>Cancel</button></>:<button disabled={!writable} onClick={()=>{setEditing(f.id);setDraft(f.value)}}>Edit English</button>}
          <button disabled={!writable||busy} onClick={()=>{const value=window.prompt("Correct English value",f.value);if(value)void mutate(current=>correctFact(novelId,f.id,{revision_id:current.revision_id,version:current.version,attribute:f.attribute,value_en:value}))}}>Correct fact…</button>
          <button disabled={!writable||busy} onClick={()=>{if(window.confirm("Remove this fact? Its source row and evidence will remain in history."))void mutate(current=>removeFact(novelId,f.id,{revision_id:current.revision_id,version:current.version}))}}>Remove</button></div>}
      </li>)}</ul>}
    </details>
    <details open><summary>Terms ({data.terms.length} occurrences)</summary>
      {data.terms.length===0?<p>No terms were recorded.</p>:<ul className="chapter-knowledge-list">{data.terms.map((t,i)=><li key={`${t.char_start}:${t.char_end}:${i}`} className={t.deleted?"knowledge-history":""}><strong>{t.source_term}</strong> → {t.target_term} {t.new_in_chapter&&<span className="knowledge-badge">New in this chapter</span>} {t.deleted&&<span className="knowledge-badge">removed</span>}
        {!t.deleted&&<span className="knowledge-actions">{editingTerm===t.source_term?<><input aria-label="Preferred English term" value={termDraft} onChange={e=>setTermDraft(e.target.value)}/><button disabled={!writable||busy||!termDraft.trim()} onClick={()=>void mutate(async()=>{await correctGlossaryTerm(novelId,t.source_term,{target_term:termDraft.trim(),at_chapter:at});setEditingTerm(null)})}>Save term</button><button onClick={()=>setEditingTerm(null)}>Cancel</button></>:<button disabled={!writable||busy} onClick={()=>{setEditingTerm(t.source_term);setTermDraft(t.target_term)}}>Edit term</button>}<button disabled={!writable||busy} onClick={()=>{if(window.confirm("Remove this term? Saved prose stays unchanged."))void mutate(()=>deleteGlossaryTerm(novelId,t.source_term,at))}}>Remove term</button></span>}</li>)}</ul>}
    </details>
  </section>;
}
