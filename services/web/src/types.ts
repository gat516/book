// Mirrors services/reader-api/models.go's JSON shapes 1:1. Keep in sync by hand — this
// is a two-service monorepo, not a shared-schema one (PLAN.md 5.1 scope).

export interface NovelSummary {
  id: string;
  title: string;
  source_lang: string;
  target_lang: string;
  genre: string | null;
  created_at: string;
}

export interface NovelListResponse {
  novels: NovelSummary[];
}

export interface CreateNovelRequest {
  title: string;
  source_lang?: string;
  target_lang?: string;
  genre?: string;
  // This novel's own LLM provider, overriding the process-wide default. The key is
  // encrypted server-side and never read back — reads report only whether one is set.
  provider_config?: SaveProviderConfigRequest;
  // Work windows. Separate because the work differs by orders of magnitude: fetching a
  // chapter is one request, translating it is a dozen-plus sequential LLM calls. 0 =
  // unlimited; omitted keeps the server default.
  ingest_lookahead?: number;
  translate_lookahead?: number;
}

export interface NovelSettings {
  novel_id: string;
  ingest_lookahead: number;
  translate_lookahead: number;
}

// ingest-api's createNovelResp, proxied verbatim by reader-api's POST /novels — NOT a
// full NovelSummary (creation returns only the generated id).
export interface CreateNovelResponse {
  id: string;
}

// ingest-api's pasteChapterReq/pasteChapterResp, proxied verbatim by reader-api's
// POST /novels/{id}/chapters.
export interface PasteChapterRequest {
  chapter_index: number;
  raw_text: string;
  translated_text?: string;
  site_chapter_no?: string;
  source_url?: string;
}

export interface PasteChapterResponse {
  novel_id: string;
  chapter_index: number;
  raw_hash: string;
  status: string;
}

// reader-api's scrapeRequest/ScrapeJobView (services/reader-api/{handlers,models}.go).
export interface StartScrapeRequest {
  start_url: string;
  mode?: "translate" | "bootstrap";
}

export interface ScrapeJobView {
  id: number;
  novel_id: string;
  start_url: string;
  mode: string;
  status: "pending" | "running" | "done" | "error" | "cancelled";
  chapters_fetched: number;
  last_error: string | null;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
}

export interface GlossaryTermView {
  // Present only when the linked entity is visible at the reader's chapter gate.
  entity_id?: string;
  source_term: string;
  target_term: string;
  version: number;
  locked_at_chapter: number;
}

export interface GlossaryResponse {
  novel_id: string;
  at: number;
  terms: GlossaryTermView[];
}

export interface VocabularyTermView {
  term_type: "attribute" | "relation";
  name: string;
  status: "candidate" | "admitted" | "banned" | "retired";
  kinds: string[];
  dst_kinds?: string[];
  cardinality: "single" | "accretive";
  polarity?: number;
  gloss?: string;
  aliases?: string[];
}

export interface VocabularyResponse {
  novel_id: string;
  at: number;
  terms: VocabularyTermView[];
}

export type VocabularyMutationAction = "admit" | "ban" | "rename-to-alias" | "set-cardinality" | "set-kinds" | "edit-gloss";
export interface VocabularyMutationRequest {
  action: VocabularyMutationAction;
  term_type: "attribute" | "relation";
  name: string;
  chapter: number;
  alias?: string;
  cardinality?: "single" | "accretive";
  kinds?: string[];
  dst_kinds?: string[];
  gloss?: string;
}

export interface CharacterNameCandidate {
  target_term: string;
  pronunciation: string[];
  segmentation: string;
  method?: "pinyin" | "restored_name" | "translated_title" | "semantic_translation";
}

export type TermRole = "chinese_person" | "foreign_person" | "personal_title" | "semantic_term";

export interface CharacterNameReview {
  source_term: string;
  first_seen_chapter: number;
  quote: string;
  reason: string;
  candidates: CharacterNameCandidate[];
  term_role: TermRole;
  rendering_method: CharacterNameCandidate["method"];
}

export interface CharacterNameReviewsResponse {
  novel_id: string;
  reviews: CharacterNameReview[];
}

// ingest-api's correctGlossaryTermReq/Resp, proxied verbatim by reader-api's
// PATCH /novels/{id}/glossary/{term}.
export interface CorrectGlossaryTermRequest {
  target_term: string;
  at_chapter: number;
}

export interface CorrectGlossaryTermResponse {
  novel_id: string;
  source_term: string;
  target_term: string;
  version: number;
}

// ingest-api's bootstrapGlossaryReq/Resp, proxied verbatim by reader-api's
// POST /novels/{id}/glossary/bootstrap (PLAN.md Phase N6). Locks a term before any
// entity for it exists — RESOLVE binds entity_id the first time it actually meets the
// surface, using this locked target_term rather than proposing its own.
export interface BootstrapGlossaryTermInput {
  source_term: string;
  target_term: string;
}

export interface BootstrapGlossaryRequest {
  terms: BootstrapGlossaryTermInput[];
}

export interface BootstrapGlossaryResponse {
  novel_id: string;
  terms: CorrectGlossaryTermResponse[];
}

// reader-api's ChapterListItem/ChapterListResponse — the paged chapter index. Metadata
// only (never chapter text), which is why it is ungated: the spoiler gate that matters
// still lives in GET /chapter/{n}.
export interface ChapterListItem {
  graph_status?: "pending" | "done" | "error";
  chapter_index: number;
  site_chapter_no?: string;
  source_url?: string;
  // Which piece of a multi-page source chapter this is (1-based; 1 when not paginated).
  // Sites that split a chapter across pages yield several rows sharing one
  // site_chapter_no, distinguished only by this.
  part: number;
  // Pipeline status: "ingested" until the worker finishes it, then "done" (readable) or
  // "error". This is what lets the UI say "still being translated" up front instead of
  // bouncing off the chapter endpoint.
  status: string;
  translation_warning: TranslationWarning | null;
}

export interface TranslationWarning { code: "locked_terms_missing"; term_count: number; }

export interface ChapterListResponse {
  novel_id: string;
  chapters: ChapterListItem[];
  total: number;
  limit: number;
  offset: number;
  progress: number;
}

// reader-api's PipelineStatusResponse — what the worker is doing right now. Read from the
// worker's Redis queue, so it reflects work in progress, which chapter.status cannot:
// that only flips once every stage has finished.
export interface InFlightChapter {
  chapter_index: number;
  stage?: string;
  // Total time since the worker claimed this chapter.
  elapsed_secs: number;
  // Time since the current stage began; resets whenever `stage` changes.
  stage_elapsed_secs: number;
}

export interface PipelineStatusResponse {
  novel_id: string;
  // Whole-queue depth, not just this novel — another novel's backlog is exactly why this
  // one might be waiting.
  pending: number;
  // Queue depth scoped to the book being viewed.
  pending_for_novel: number;
  // Published by the worker with a short TTL, including while a model call is running.
  worker_online: boolean;
  queue_mode: "all" | "focused" | "paused";
  in_flight: InFlightChapter[];
}

// reader-api's TranslationHealth — whether the model is naming things consistently.
// `warn` is the server's judgement so every client applies the same threshold.
export interface TranslationHealth {
  novel_id: string;
  locked_terms: number;
  unstable_terms: number;
  failed_chapters: number;
  warning_chapters: number;
  warn: boolean;
  reason?: string;
}

export interface KnowledgeStatus {
  revision_id: string; version: number; trusted: boolean; status: string;
  // Mirror reader-api's KnowledgeStatus (migration 0058). Meaningful only for the entity
  // graph's own `knowledge` field -- `event_knowledge` shares this type but always reads
  // false here, since events have no per-chapter extraction gate of their own.
  legacy: boolean; chapter_snapshotted: boolean; can_extract: boolean;
}
export interface Evidence { id: string; chapter: number; quote: string; source_hash: string; char_start: number; char_end: number; }
export interface Relationship { id: number; relation: string; direction: string; entity: EntitySummary; source_chapter: number; evidence?: Evidence | null; }
export interface SpanView {
  mention_id?: string;
  source_mention_id?: string | null;
  evidence?: Evidence | null;
  known_from_chapter?: number | null;
  enrichment_status?: string;
  // A source-term rendering review matched to this literal display span. This can exist
  // before identity resolution, so it deliberately does not imply entity_id.
  rendering?: TermRenderingView;
  // A literal named mention can have a card before its identity is linked.
  entity_id: string | null;
  char_start: number;
  char_end: number;
}

export interface ChapterResponse {
  knowledge: KnowledgeStatus;
  event_knowledge: KnowledgeStatus;
  novel_id: string;
  chapter_index: number;
  // The reader's STORED PROGRESS (not chapter_index) — see HoverCard.tsx for why this
  // exact value, captured once per chapter load, is what every hover/ask on this
  // chapter view must use as `at`.
  at: number;
  text: string;
  spans: SpanView[];
  // Facts whose source_chapter is exactly this chapter — what the reader learns HERE.
  // Everything learned earlier stays on the entity card, fetched on demand.
  new_facts: ChapterFactView[];
  events: EventView[];
  has_next: boolean;
  translation_warning: TranslationWarning | null;
  // The source site's own printed chapter label (e.g. "第4610章"), when this chapter came
  // from a scrape — NOT the same number as chapter_index, which is our own sequential
  // counter for this ingestion batch. Absent for a plain paste with no site of origin.
  site_chapter_no?: string;
  // Durable original page URL, available even when scraper/pipeline workers are stopped.
  source_url?: string;
  // Which piece of a multi-page source chapter this is (1-based; 1 when not paginated).
  part: number;
}

// One fact introduced by this chapter, keyed to the entity it describes. Served on the
// chapter response so the reader pane can badge a mention without a round trip per span.
export interface ChapterFactView {
	id: number;
  entity_id: string;
	entity_canonical: string;
  attribute: string;
  value: string;
	value_source: string;
	value_en: string | null;
	kind: "assertion" | "correction" | "retraction";
	supersedes?: number;
	status: "active" | "superseded" | "retracted";
	evidence: Evidence | null;
  valid_from_chapter: number;
  source_chapter: number;
  confidence: number;
}

export interface ChapterTermView {
  source_term: string; target_term: string; char_start: number; char_end: number;
  new_in_chapter: boolean; deleted: boolean;
}
export interface ChapterKnowledgeRun {
  id: string; mode: "ordinary" | "reextract";
  scope: "all" | "terms" | "facts";
  state: "pending" | "processing" | "awaiting_review" | "applying" | "published" | "rejected" | "failed";
  created_at: string; preview?: { items: ReextractPreviewItem[] };
}
export interface ReextractPreviewItem {
  item_key: string; item_kind: "fact" | "term";
  classification: "unchanged" | "display_update" | "new" | "possible_replacement" | "missing";
  existing_fact_id?: number; proposal?: Record<string, unknown>;
}
export interface ChapterGraphExtraction {
  state: "pending" | "processing" | "done" | "failed";
  verified_terms: number;
  verified_claims: number;
  published_fact_rows: number;
}
export interface ChapterKnowledgeResponse {
  novel_id: string; chapter_index: number; revision_id: string; version: number;
  trusted: boolean; status: string;
  // The real predicate ingest-api enforces before a per-chapter extraction may run
  // (migration 0058): trusted alone says "readable", not "writable" -- every never-
  // rebuilt book is legacy=true, trusted=true, and cannot take one yet.
  legacy: boolean; chapter_snapshotted: boolean; can_extract: boolean;
  // "" when writable; otherwise the one cause, matched to a KnowledgeGate case:
  // "never_built" | "quarantined" | "chapter_not_snapshotted".
  blocked_reason: string;
  terms_extracted: boolean; facts_extracted: boolean;
  facts: ChapterFactView[]; terms: ChapterTermView[]; run?: ChapterKnowledgeRun;
  graph_extraction?: ChapterGraphExtraction;
}
export interface ChapterKnowledgeActivity {
  sequence: number; run_id: string; item_kind: "fact" | "term" | "run";
  item_key: string; phase: "detected" | "proposed" | "verified" | "published" | "rejected";
  payload: Record<string, unknown>; created_at: string;
}

// One row of reader_held_knowledge (migration 0074). Exactly one of {attribute, rel_type}
// and one of {entity_id, (src_id,dst_id)} is populated depending on item_type — never
// both, and never guessed on the client.
export interface HeldKnowledgeItem {
  item_type: "fact" | "edge" | "event";
  item_id: number;
  revision_id: string;
  revision_version: number;
  chapter_index: number;
  entity_id: string | null;
  src_id: string | null;
  dst_id: string | null;
  attribute: string | null;
  rel_type: string | null;
  value: string | null;
  summary: string | null;
  evidence_id: string | null;
  evidence_quote: string | null;
  review_state: "held" | "passed" | "rejected";
  review_flag: string | null;
  // Mirrors the exact set pass_all_corroborated would apply server-side (migration 0076's
  // corroborated_fact_ids, shared by this preview and the write). Always false for
  // edge/event items.
  bulk_eligible: boolean;
}

export interface HeldKnowledgeResponse {
  novel_id: string;
  chapter_index: number;
  at: number;
  items: HeldKnowledgeItem[];
}

export interface KnowledgeReviewItem {
  item_type: "fact" | "edge" | "event";
  id: number;
  verdict: "pass" | "reject";
  reason: string;
}

// The browser collects verdicts and reasons only; every gate (stale version, scope,
// idempotency, corroboration) is server-side (CLAUDE.md: "Go never reimplements a gate").
export interface KnowledgeReviewRequest {
  revision_id: string;
  version: number;
  request_id: string;
  items?: KnowledgeReviewItem[];
  pass_all_corroborated?: boolean;
}

export interface KnowledgeReviewOutcome {
  item_type: string;
  id: number;
  verdict: string;
  already_applied: boolean;
}

export interface KnowledgeReviewResponse {
  revision_id: string;
  version: number;
  applied: KnowledgeReviewOutcome[];
}

export interface FactView {
  evidence?: Evidence | null;
  attribute: string;
  value: string;
  valid_from_chapter: number;
  source_chapter: number;
  confidence: number;
}

export interface EntitySummary {
  id: string;
  canonical: string;
  kind: string;
  first_seen_chapter: number;
}

export interface EventArgumentView {
  role: string;
  surface: string;
  entity_id: string | null;
  entity?: EntitySummary;
}

export interface EventView {
  id: string;
  chapter_index: number;
  event_type: string;
  action: string;
  status: "completed" | "attempted" | "prevented";
  summary: string;
  result: string | null;
  arguments: EventArgumentView[];
  entities: EntitySummary[];
  evidence: Evidence;
}

export interface TimelineResponse {
  knowledge: KnowledgeStatus;
  event_knowledge: KnowledgeStatus;
  novel_id: string;
  at: number;
  events: EventView[];
}

export interface EntityView extends EntitySummary {
  knowledge: KnowledgeStatus;
  aliases: string[];
  facts: FactView[];
  renderings: TermRenderingView[];
}

export interface TermRenderingView {
  source_term: string;
  target_term: string | null;
  status: "pending" | "unlocked" | "locked";
  term_role: TermRole | "";
  candidates: CharacterNameCandidate[];
}

export interface EntityResponse {
  novel_id: string;
  at: number;
  entity: EntityView;
}

export interface RetrievedSource {
  kind: "chunk" | "fact" | "edge" | "event";
  id: number | string;
  chapter: number;
}

export interface AskResponse {
  answer: string;
  at: number;
  retrieved_sources: RetrievedSource[];
  served_by: { provider: string; model: string } | null;
  knowledge?: KnowledgeStatus;
  event_knowledge?: KnowledgeStatus;
}

export interface Progress {
  novel_id: string;
  reader_id: string;
  current_chapter: number;
  updated_at: string;
}


export type ProviderName = "anthropic" | "deepseek" | "gemini" | "ollama";

// Write shape. api_key is plaintext in transit and encrypted (AES-GCM) before it reaches
// Postgres; it is never stored or returned as such.
export interface SaveProviderConfigRequest {
  provider: ProviderName;
  model?: string;
  translate_model?: string;
  extract_model?: string;
  base_url?: string;
  api_key?: string;
}

// Read shape (ingest-api's ProviderConfigView). Deliberately asymmetric with the write
// shape: the key is never read back, only whether one exists.
export interface ProviderConfigView {
  provider: ProviderName;
  model?: string;
  translate_model?: string;
  extract_model?: string;
  base_url?: string;
  api_key_set: boolean;
}


// One shared credential per provider (migration 0035): entered once in Settings and used
// by every book, unless a book carries its own key as an override. Read shape is masked
// the same way ProviderConfigView is -- api_key_set, never the key.
export interface ProviderCredentialView {
  provider: ProviderName;
  base_url?: string;
  api_key_set: boolean;
}

export interface ProviderCredentialsResponse {
  credentials: ProviderCredentialView[];
}

export interface SaveProviderCredentialRequest {
  base_url?: string;
  api_key?: string;
}


// Knowledge repair (reader-api's repair.go). Two independent tracks: the entity graph and
// chapter events each have their own activation pointer, so one can be quarantined while
// the other is fine.
export type RepairState =
  | "ready"
  | "quarantined"
  | "rebuilding"
  | "awaiting_review"
  | "failed"
  | "unavailable";

export interface RepairFailure {
  chapter_index: number;
  attempts: number;
  // A safe class, never the stored exception text -- that can carry source prose or a
  // connection string, so reader-api maps it server-side and `detail` is a fixed sentence.
  category: string;
  detail: string;
  retry_at: string | null;
  occurred_at: string;
}

export interface RepairReplacement {
  revision_id: string;
  model?: string;
  prompt_version?: string;
  created_at?: string;
  // From the frozen preview report, not the API's own judgement. null = not yet reported.
  activation_eligible: boolean | null;
  review_hash?: string;
  // True once record_review has stored derived metrics. It clears `review` as it does so,
  // so `reviewed: true` with no review_hash means "reviewed, waiting for a fresh report".
  reviewed: boolean;
}

export interface RepairRollbackTarget {
  revision_id: string;
  // Rolling back to an untrusted revision does NOT restore its facts -- switch preserves
  // trust deliberately -- so this has to be visible at the point of choosing.
  trusted: boolean;
  created_at: string;
}

export interface RepairChapters {
  total: number;
  done: number;
  failed: number;
  running: number;
}

export interface RepairTrack {
  state: RepairState;
  // The server's sentence. Render it rather than re-deriving prose from the numbers, so
  // every surface says the same thing -- same split as TranslationHealth's `reason`.
  reason: string;
  active_revision?: string;
  active_trusted: boolean;
  // Claims stored on the active revision that readers currently cannot see. This is the
  // number that makes "facts unavailable" concrete instead of ambiguous.
  withheld_claims: number;
  // Describes whichever revision is currently doing work: the replacement when one is
  // being built, otherwise the active revision's own enrichment.
  chapters: RepairChapters;
  replacement: RepairReplacement | null;
  // Archived revisions rollback may actually target. switch() refuses anything else, so
  // the active revision must never appear here.
  rollback_targets: RepairRollbackTarget[];
  // Earlier staging revisions superseded by a later restart. A large number is the
  // "this book keeps needing rebuilds" signal.
  superseded: number;
  failures: RepairFailure[];
  // False once every failure has exhausted its attempts: waiting is no longer a strategy.
  retryable: boolean;
  // Why the run cannot proceed at all, as opposed to one chapter failing. Its absence
  // used to be indistinguishable from "working slowly".
  blocked: RepairBlocked | null;
  // claims/entities only land when a WHOLE chapter publishes, so they move at the same
  // moment chapters.done does. calls is one per completed model call, several per
  // chapter -- the only counter that moves *inside* a chapter, and so the only honest
  // "still alive" signal on hardware where one call can take ten minutes.
  published: { claims: number; entities: number; calls: number };
  // Content-free graph_job telemetry. The separate Redis heartbeat says whether the
  // process is alive; these timestamps say whether this particular model call is moving.
  worker?: RepairWorkerReport;
  // Which chapter is being read right now. "0 of 26" cannot tell you whether it is stuck
  // on the first chapter or working through the twentieth.
  current: { chapter: number; since: string } | null;
}

export interface RepairWorkerReport {
  job_state: "pending" | "processing" | "done" | "failed";
  stage?: string;
  stage_started_at?: string;
  last_progress_at?: string;
  attempts: number;
  category?: string;
  detail?: string;
  updated_at: string;
}

export interface RepairBlocked {
  category: string;
  detail: string;
  since: string;
  // Present only for the two causes the worker treats as transient (an unreachable
  // endpoint or a timed-out call) -- it will retry on its own by this time. Absent for
  // every other cause, which never self-clears.
  retry_eligible_at?: string;
}

// One claim the running rebuild has already published. Operator-only: unreviewed, and
// quoted from anywhere in the book.
// A surface the model proposed that nothing has published yet -- unreviewed, with its
// source quote. Operator-only.
export interface RepairExtractedName {
  surface: string;
  kind: string;
  named: boolean;
  quote?: string;
  // The locked glossary rendering for this surface, when one already exists. Absent for
  // a name nobody has glossed yet.
  target_term?: string;
}

// A fact the model has proposed but that nothing has published. It has not passed mention
// resolution, graph_write's literal-evidence check, or review; some never become facts.
export interface RepairProposedClaim {
  kind: string;
  attribute: string;
  value: string;
  quote?: string;
  mentions: number;
}

export interface RepairProgressFact {
  id: number;
  entity: string;
  kind: string;
  attribute: string;
  value: string;
  chapter_index: number;
  quote?: string;
}

export type RepairTrackName = "graph" | "events";

// "review"/"activate" now apply only to the structured-event track; the entity graph
// uses "adopt"/"quarantine" instead (Phase E, migration 0077) -- pipeline/repair.py's
// dispatch enforces the split, not this type.
export type RepairAction = "prepare" | "review" | "activate" | "rollback" | "discard" | "extend" | "reextract" | "reextract_apply" | "adopt" | "quarantine";

export interface RepairRequestView {
  id: string;
  track: RepairTrackName;
  action: RepairAction;
  state: "pending" | "running" | "done" | "failed";
  attempts: number;
  category?: string;
  // Server-rendered sentence for `category` -- render this, not the category slug.
  detail?: string;
  // Set only while pending and backing off after a failed attempt -- absent for a freshly
  // queued request with nothing to retry yet.
  retry_at?: string;
  requested_by: string;
  created_at: string;
  updated_at: string;
}

export interface RepairAuditEntry {
  track: RepairTrackName;
  action: string;
  created_at: string;
}

export interface RepairStatus {
  novel_id: string;
  graph: RepairTrack;
  events: RepairTrack;
  // Actions asked for through the UI. A click does not act instantly -- repair runs on
  // the worker's idle tick so it loses to reader-critical translation -- so `pending`
  // here is the honest thing to show rather than pretending the action already happened.
  requests: RepairRequestView[];
  // graph_audit/event_audit: has this book been repaired before, and how often.
  history: RepairAuditEntry[];
}

// The frozen review report. Operator-only: it carries source quotes from every chapter in
// the snapshot regardless of reading progress. Its inner shape is defined by the Python
// that produces it (graph_rebuild.preview), so it stays loosely typed here rather than
// becoming a second definition to keep in sync.
export interface RepairPreview {
  revision_id: string;
  version: number;
  state: string;
  report: RepairReport | null;
}

export interface RepairReportMention {
  id: string;
  chapter?: number;
  surface?: string;
  surface_target?: string | null;
  kind?: string;
  entity?: string | null;
  entity_source?: string | null;
  quote?: string;
  target_context?: string;
}

export interface RepairReportClaim {
  id: number;
  entity?: string;
  entity_source?: string;
  attribute?: string;
  value?: string;
  chapter?: number;
  quote?: string;
  target_context?: string;
}

export interface RepairReport {
  review_hash?: string;
  activation_eligible?: boolean;
  completed_jobs?: number;
  total_jobs?: number;
  saved_prose_unchanged?: boolean;
  glossary_unchanged?: boolean;
  evidence_valid?: boolean;
  mention_coverage?: { total: number; linked: number; unresolved: number };
  mentions?: RepairReportMention[];
  claims?: RepairReportClaim[];
  failures?: unknown[];
  rejected?: unknown[];
  identity_changes?: unknown[];
  [key: string]: unknown;
}

// What the browser submits back. The client collects booleans and nothing else: scores
// are derived server-side by record_review from these plus the actually-stored bindings,
// which is what makes the activation gate meaningful.
export interface RepairReviewDocument {
  review_hash: string;
  reviewer: string;
  approved: boolean;
  known_merge_regressions: number;
  mentions: { id: string; correct: boolean; unambiguous: boolean }[];
  facts: { id: number; correct: boolean }[];
}
