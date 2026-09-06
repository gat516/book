import { useCallback, useEffect, useState } from "react";
import {
  applyChapterReextract, correctFact, correctGlossaryTerm, deleteGlossaryTerm,
  editFactDisplay, getChapterKnowledge, getChapterKnowledgeActivity, getProviderConfig,
  getRepairStatus, listOllamaModels, removeFact, requestRepair, startChapterReextract,
} from "../api";
import { defaultGraphExtractModel } from "../providers";
import type { ChapterKnowledgeActivity, ChapterKnowledgeResponse, RepairStatus } from "../types";
import { RepairReview } from "./RepairReview";

const terminal = new Set(["published", "rejected", "failed", "awaiting_review"]);

function shortId(id: string) { return id.slice(0, 8); }

// The one-time build a never-rebuilt or not-yet-included book needs: KnowledgeEngine
// requires a pinned model and a snapshot neither a legacy revision nor a missing chapter
// has. Adopting a legacy revision instead of building one would have to fabricate the
// graph_evidence rows the literal-evidence check depends on (§0: structural, not
// prompted), so the one-time build is the honest path. Shared by the never_built and
// chapter_not_snapshotted gates below, which differ only in copy and button label.
function BuildGraph({ novelId, label, onDone }: { novelId: string; label: string; onDone: () => Promise<unknown> }) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void listOllamaModels(novelId).then(setModels).catch(() => setModels([]));
    void getProviderConfig(novelId)
      .then((config) => {
        const preferred = defaultGraphExtractModel(config);
        if (preferred) setModel((current) => current || preferred);
      })
      .catch(() => undefined);
  }, [novelId]);

  async function build() {
    setBusy(true); setError("");
    try {
      await requestRepair(novelId, { track: "graph", action: "prepare", params: { model } });
      await onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="knowledge-gate-action">
      <label>Model{" "}
        <select value={model} onChange={(e) => setModel(e.target.value)} disabled={models.length === 0}>
          <option value="">{models.length === 0 ? "No models found" : "Choose a model…"}</option>
          {models.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </label>
      <button disabled={busy || !model} onClick={() => void build()}>{label}</button>
      {error && <p role="alert" className="reader-pane-error">{error}</p>}
    </div>
  );
}

// A rebuild left unfinished withholds facts until it is finished, activated or
// discarded. `discard` is the escape prepare()'s precautionary quarantine never had: it
// undoes THAT quarantine when nothing since has re-quarantined the same revision,
// restoring the earlier facts; otherwise it says plainly that they stay withheld.
function QuarantineGate({ novelId, onDone }: { novelId: string; onDone: () => Promise<unknown> }) {
  const [status, setStatus] = useState<RepairStatus | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(
    () => getRepairStatus(novelId).then(setStatus).catch((e) => setError(String(e))),
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
      setError(String(e));
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
      <BuildGraph novelId={novelId} label="Start a rebuild" onDone={async () => { await load(); await onDone(); }} />
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
      {track.blocked && <p role="alert" className="reader-pane-error">Stalled: {track.blocked.detail}</p>}
      <div className="knowledge-actions">
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
function KnowledgeGate({ novelId, reason, onDone }: { novelId: string; reason: string; onDone: () => Promise<unknown> }) {
  if (reason === "never_built") {
    return <div className="knowledge-gate">
      <p>No chapter-knowledge graph yet. Building it reads every finished chapter once;
        after that you can extract any chapter on its own.</p>
      <BuildGraph novelId={novelId} label="Build chapter knowledge" onDone={onDone} />
    </div>;
  }
  if (reason === "quarantined") {
    return <div className="knowledge-gate"><QuarantineGate novelId={novelId} onDone={onDone} /></div>;
  }
  if (reason === "chapter_not_snapshotted") {
    return <div className="knowledge-gate">
      <p>This chapter was added after the graph was built, so there is nothing to append it to yet.</p>
      <BuildGraph novelId={novelId} label="Rebuild to include it" onDone={onDone} />
    </div>;
  }
  return null;
}

export function ChapterKnowledgeWorkspace({novelId,chapter,at}:{novelId:string;chapter:number;at:number}) {
  const [data,setData]=useState<ChapterKnowledgeResponse|null>(null);
  const [activity,setActivity]=useState<ChapterKnowledgeActivity[]>([]);
  const [error,setError]=useState(""); const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState<number|null>(null); const [draft,setDraft]=useState("");
  const [editingTerm,setEditingTerm]=useState<string|null>(null); const [termDraft,setTermDraft]=useState("");
  const [decisions,setDecisions]=useState<Record<string,string>>({});
  const load=useCallback(async()=>{const next=await getChapterKnowledge(novelId,chapter);setData(next);return next},[novelId,chapter]);

  useEffect(()=>{setData(null);setActivity([]);setError("");void load().catch(e=>setError(String(e)))},[load]);
  useEffect(()=>{
    const run=data?.run;if(!run)return;let cancelled=false;let timer:number|undefined;
    async function poll(){try{const after=activity.at(-1)?.sequence??0;const response=await getChapterKnowledgeActivity(novelId,chapter,run!.id,after);if(cancelled)return;if(response.activity.length)setActivity(old=>[...old,...response.activity]);const fresh=await load();if(!terminal.has(fresh.run?.state??""))timer=window.setTimeout(poll,2000)}catch{if(!cancelled)timer=window.setTimeout(poll,5000)}}
    void poll();return()=>{cancelled=true;if(timer)clearTimeout(timer)};
    // sequence is deliberately read when each poll runs; restarting on every item duplicates requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[data?.run?.id,data?.run?.state,novelId,chapter,load]);

  // A chapter can finish background publication while this panel is open. Read the
  // current graph fence immediately before every write; §0 still rejects a genuinely
  // concurrent change, but a merely old browser snapshot no longer causes a false stale edit.
  async function mutate(work:(current:ChapterKnowledgeResponse)=>Promise<unknown>){setBusy(true);setError("");try{const current=await load();await work(current);await load()}catch(e){try{await load()}catch{/* retain the mutation error */}setError(String(e))}finally{setBusy(false)}}
  if(!data)return <section className="chapter-knowledge"><h2>Chapter knowledge</h2><p>{error||"Loading knowledge…"}</p></section>;
  const preview=data.run?.preview?.items??[];
  const writable=data.can_extract;
  return <section className="chapter-knowledge" aria-labelledby="chapter-knowledge-heading">
    <header><div><h2 id="chapter-knowledge-heading">Chapter knowledge</h2><p>Extract terms finds this chapter's named terms; extract facts finds the claims it supports. Proposed items stay out of cards and Ask AI until published.</p></div>
      <span className="knowledge-actions"><button disabled={!writable||busy||!!data.run&&!terminal.has(data.run.state)} onClick={()=>void mutate(()=>startChapterReextract(novelId,chapter,"terms"))}>Extract terms</button><button disabled={!writable||busy||!!data.run&&!terminal.has(data.run.state)} onClick={()=>void mutate(()=>startChapterReextract(novelId,chapter,"facts"))}>Extract facts</button></span></header>
    {error&&<p role="alert" className="reader-pane-error">{error}</p>}
    {!writable&&<KnowledgeGate novelId={novelId} reason={data.blocked_reason} onDone={load} />}
    <details open><summary>Activity {data.run&&`— ${data.run.scope} ${data.run.state.replace("_"," ")}`}</summary>
      {activity.length===0?<p>No active extraction activity.</p>:<ol className="knowledge-activity">{activity.map(a=><li key={a.sequence}><time>{new Date(a.created_at).toLocaleTimeString()}</time> <span className={`knowledge-badge phase-${a.phase}`}>{a.phase}</span> {a.item_kind} {a.phase==="proposed"&&<em> — unverified</em>}</li>)}</ol>}
      {data.run?.state==="awaiting_review"&&<div className="knowledge-review"><h3>{data.run.scope==="terms"?"Term extraction review":"Fact extraction review"}</h3><p>{data.run.scope==="terms"?"Publish verified term occurrences. This does not extract or change facts.":"Nothing below changes the graph unless you explicitly select it. Missing model claims default to retain."}</p>
        {preview.map(item=><label key={item.item_key}><span>{item.item_kind}: {item.classification.replace("_"," ")}</span>{item.item_kind==="term"?<small>Verified terms will be published; the term itself remains editable below.</small>:<select value={decisions[item.item_key]??"retain"} onChange={e=>setDecisions(old=>({...old,[item.item_key]:e.target.value}))}><option value="retain">Retain current knowledge</option>{item.classification==="new"&&<option value="approve">Publish new item</option>}{item.classification==="display_update"&&<option value="update_display">Update English display</option>}{item.classification==="possible_replacement"&&<option value="replace">Publish correction</option>}{item.classification==="missing"&&<option value="remove">Publish retraction</option>}</select>}</label>)}
        <button disabled={busy} onClick={()=>void mutate(current=>applyChapterReextract(novelId,chapter,current.run?.id||data.run!.id,{revision_id:current.revision_id,version:current.version,decisions}))}>{data.run.scope==="terms"?"Publish verified terms":Object.values(decisions).some(v=>v!=="retain")?"Apply selected changes":"Finish review — retain everything"}</button></div>}
    </details>
    <details open><summary>Facts ({data.facts.length})</summary>
      {data.facts.length===0?<p>No published facts originated here.</p>:<ul className="chapter-knowledge-list">{data.facts.map(f=><li key={f.id} className={f.status!=="active"?"knowledge-history":""}>
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
