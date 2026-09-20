// Mirrors services/reader-api/models.go's JSON shapes 1:1. Keep in sync by hand — this
// is a two-service monorepo, not a shared-schema one (PLAN.md 5.1 scope).

export interface NovelSummary {
  id: string;
  title: string;
  source_lang: string;
  target_lang: string;
  genre: string | null;
  created_at: string;
  current_chapter?: number;
}

// One chapter's FACTS progress (reader-api FactsStatus). Never fact text.
export interface FactsStatus {
  state: "pending" | "processing" | "ready" | "failed" | string;
  failure_detail?: string | null;
  retry_attempts?: number;
  retry_max_attempts?: number;
  retry_at?: string | null;
  retry_category?: string | null;
  // Facts work explicitly paused for this chapter.
  discarded?: boolean;
  // Facts the FACTS stage wrote for this chapter. Absent until the stage has run.
  facts_count?: number | null;
}
export interface ChapterFactsStatusResponse { novel_id: string; chapter_index: number; at: number; status: FactsStatus; }
// Book-wide facts progress (ingest-api FactsStatus).
export interface BookFactsStatus {
  novel_id: string;
  eligible_chapters: number;
  done_chapters: number;
  missing_chapters: number;
  // Facts work for the book is queued, claimed, or waiting on a scheduled retry.
  running: boolean;
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

// What one page extracts to, read by the scraper exactly as a scrape would (scraper/preview.go).
export interface ScrapePreview {
  url: string;
  host: string;
  reader: "built-in" | "generic" | string;
  robots_allowed: boolean;
  excerpt?: string;
  title?: string;
  text_chars: number;
  paragraphs: number;
  next_url?: string;
  continues: boolean;
  suggested_mode?: "translate" | "bootstrap";
  error?: string;
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
// chapter is translated.
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
  // Reader-feature work finishes after the chapter becomes readable. Keeping it
  // separate lets the chapter list distinguish "ready to read" from "cards and AskAI
  // are still being prepared."
  graph_status?: "pending" | "done" | "error" | string;
  // Safe category derived server-side from chapter_failure.error_code. Raw diagnostics
  // never appear in the ungated chapter index.
  failure_category?: string;
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

export type ProviderHealthTrack = "graph" | "events" | "translate" | "extract";

export interface ProviderHealth {
  provider: ProviderName;
  endpoint_kind: "local" | "hosted";
  state: "serving" | "unavailable";
  category:
    | "ok"
    | "unreachable"
    | "credential_missing"
    | "credential_rejected"
    | "rate_limited"
    | "quota_exhausted"
    | "provider_retry_exhausted"
    | "model_not_available"
    | "model_server_error"
    | "unknown";
}

export interface Evidence { id: string; chapter: number; quote: string; source_hash: string; char_start: number; char_end: number; }
export interface Relationship { id: number; relation: string; direction: string; entity: EntitySummary; source_chapter: number; evidence?: Evidence | null; }
export interface SpanView {
  mention_id?: string;
  source_mention_id?: string | null;
  evidence?: Evidence | null;
  known_from_chapter?: number | null;
  // A source-term rendering review matched to this literal display span.
  rendering?: TermRenderingView;
  char_start: number;
  char_end: number;
}

export interface ChapterResponse {
  novel_id: string;
  chapter_index: number;
  // Stored progress (not chapter_index). Spelling writes use this authorized boundary;
  // story wiki and Ask AI narrow it to the open chapter during a reread (§0.3).
  at: number;
  text: string;
  spans: SpanView[];
  // This chapter's FACTS progress, for the first paint of the status line.
  facts_status?: FactsStatus;
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

export interface EntitySummary {
  id: string;
  canonical: string;
  kind: string;
  first_seen_chapter: number;
}

export interface TermRenderingView {
  source_term: string;
  target_term: string | null;
  status: "pending" | "unlocked" | "locked";
  term_role: TermRole | "";
  candidates: CharacterNameCandidate[];
}

export interface RetrievedSource {
  kind: "chunk" | string;
  id: number | string;
  chapter: number;
}

export interface AskResponse {
  answer: string;
  at: number;
  retrieved_sources: RetrievedSource[];
  served_by: { provider: string; model: string } | null;
}

export interface Progress {
  novel_id: string;
  reader_id: string;
  current_chapter: number;
  updated_at: string;
}

export type ProviderName = "anthropic" | "custom" | "deepseek" | "gemini" | "groq" | "ollama";

// Write shape. Carries no secret: a book names a provider, and the key for that provider
// is entered once in Settings (ProviderCredentialView) rather than per book (0080).
export interface SaveProviderConfigRequest {
  provider: ProviderName;
  model?: string;
  translate_model?: string;
  extract_model?: string;
  facts_model?: string;
  base_url?: string;
}

// Read shape (ingest-api's ProviderConfigView), now symmetric with the write shape --
// every field here is one the client sent and can send back unchanged.
export interface ProviderConfigView {
  provider: ProviderName;
  model?: string;
  translate_model?: string;
  extract_model?: string;
  facts_model?: string;
  base_url?: string;
}

// One credential per provider (migrations 0035, 0080): entered once in Settings and used
// by every book that names that provider. This is now the only shape that carries a key,
// and the only masked read left -- api_key_set, never the key itself.
export type CredentialProviderName = ProviderName | "openrouter";

export interface EmbeddingConfig {
  provider: "auto" | "disabled" | "server" | "gemini" | "openrouter";
  model: string;
}

export interface ProviderCredentialView {
  provider: CredentialProviderName;
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

// A character's wiki page as of the reader's chapter (reader-api /wiki/pages): the
// characters met so far, and one character's tagged facts with names filled in.
// A wiki subject's kind (migration 0115). Events are a timeline, not a kind of page.
export type SubjectKind = "character" | "organization" | "place" | "item";
export interface WikiPageSummary { subject: string; source_term?: string; title: string; kind: SubjectKind; facts: number; }
export interface WikiPagesResponse { novel_id: string; at: number; pages: WikiPageSummary[]; }
export interface WikiFact { chapter: number; category: string; kind?: string | null; text: string; subjects: string[]; version: string; ordinal: number; }
// names/kinds cover every subject the facts name, so a page can link to theirs.
export interface WikiPageResponse { novel_id: string; at: number; subject: string; title: string; kind: SubjectKind; facts: WikiFact[]; names: Record<string, string>; kinds: Record<string, SubjectKind>; }
export interface WikiEventsResponse { novel_id: string; at: number; facts: WikiFact[]; names: Record<string, string>; kinds: Record<string, SubjectKind>; }
