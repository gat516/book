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
  provider_config?: {
    provider: "anthropic" | "deepseek" | "ollama";
    model?: string;
    base_url?: string;
    api_key?: string;
  };
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
  chapter_index: number;
  site_chapter_no?: string;
  // Which piece of a multi-page source chapter this is (1-based; 1 when not paginated).
  // Sites that split a chapter across pages yield several rows sharing one
  // site_chapter_no, distinguished only by this.
  part: number;
  // Pipeline status: "ingested" until the worker finishes it, then "done" (readable) or
  // "error". This is what lets the UI say "still being translated" up front instead of
  // bouncing off the chapter endpoint.
  status: string;
}

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
  elapsed_secs: number;
}

export interface PipelineStatusResponse {
  novel_id: string;
  // Whole-queue depth, not just this novel — another novel's backlog is exactly why this
  // one might be waiting.
  pending: number;
  in_flight: InFlightChapter[];
}

// reader-api's TranslationHealth — whether the model is naming things consistently.
// `warn` is the server's judgement so every client applies the same threshold.
export interface TranslationHealth {
  novel_id: string;
  locked_terms: number;
  unstable_terms: number;
  failed_chapters: number;
  warn: boolean;
  reason?: string;
}

export interface SpanView {
  entity_id: string;
  char_start: number;
  char_end: number;
}

export interface ChapterResponse {
  novel_id: string;
  chapter_index: number;
  // The reader's STORED PROGRESS (not chapter_index) — see HoverCard.tsx for why this
  // exact value, captured once per chapter load, is what every hover/ask on this
  // chapter view must use as `at`.
  at: number;
  text: string;
  spans: SpanView[];
  has_next: boolean;
  // The source site's own printed chapter label (e.g. "第4610章"), when this chapter came
  // from a scrape — NOT the same number as chapter_index, which is our own sequential
  // counter for this ingestion batch. Absent for a plain paste with no site of origin.
  site_chapter_no?: string;
  // Which piece of a multi-page source chapter this is (1-based; 1 when not paginated).
  part: number;
}

export interface FactView {
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

export interface EntityView extends EntitySummary {
  aliases: string[];
  facts: FactView[];
}

export interface EntityResponse {
  novel_id: string;
  at: number;
  entity: EntityView;
}

export interface RetrievedSource {
  kind: "chunk" | "fact" | "edge";
  id: number;
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
