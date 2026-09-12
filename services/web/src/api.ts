import { readerId } from "./readerId";
import type {
  AskResponse,
  BootstrapGlossaryRequest,
  BootstrapGlossaryResponse,
  ChapterListResponse,
  CharacterNameReviewsResponse,
  ChapterResponse,
  CorrectGlossaryTermRequest,
  CorrectGlossaryTermResponse,
  CreateNovelRequest,
  CreateNovelResponse,
  EntityResponse,
  GlossaryResponse,
  NovelListResponse,
  NovelSummary,
  PipelineStatusResponse,
  TranslationHealth,
  VocabularyMutationRequest,
  VocabularyResponse,
  PasteChapterRequest,
  ProviderConfigView,
  ProviderCredentialsResponse,
  SaveProviderCredentialRequest,
  SaveProviderConfigRequest,
  PasteChapterResponse,
  Progress,
  ScrapeJobView,
  StartScrapeRequest,
  TimelineResponse,
  RecordsResponse,
  RecordsInspectorResponse,
  WikiResponse,
} from "./types";

class ApiError extends Error {
  constructor(
    public status: number,
    // reader-api's error envelope is always {"error": "..."} (writeError in handlers.go).
    public code: string,
    public body?: unknown,
  ) {
    super(code);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "X-Reader-ID": readerId(),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    let code = body || response.statusText;
    let parsed: unknown;
    try {
      parsed = JSON.parse(body);
      if (parsed && typeof parsed === "object" && "error" in parsed) {
        const value = (parsed as { error?: unknown }).error;
        if (typeof value === "string") code = value;
      }
    } catch {
      // non-JSON error body: fall back to the raw text set above
    }
    throw new ApiError(response.status, code, parsed);
  }
  return response.json() as Promise<T>;
}

// Ungated on the server (novel metadata has no source_chapter to gate on) — the
// X-Reader-ID header sent by `request()` is simply ignored by reader-api for these.
export function listNovels(): Promise<NovelListResponse> {
  return request(`/novels`);
}

export function getNovel(novelId: string): Promise<NovelSummary> {
  return request(`/novels/${novelId}`);
}

export function createNovel(body: CreateNovelRequest): Promise<CreateNovelResponse> {
  return request(`/novels`, { method: "POST", body: JSON.stringify(body) });
}

// Irreversible: ingest-api cascades this to the novel's chapters, graph, glossary and
// queued work (migration 0030). The caller is responsible for confirming with the user.
export function deleteNovel(novelId: string): Promise<{ deleted: boolean }> {
	return request(`/novels/${novelId}`, { method: "DELETE" });
}

export function pasteChapter(novelId: string, body: PasteChapterRequest): Promise<PasteChapterResponse> {
  return request(`/novels/${novelId}/chapters`, { method: "POST", body: JSON.stringify(body) });
}

export function getChapter(novelId: string, n: number): Promise<ChapterResponse> {
  return request(`/novels/${novelId}/chapter/${n}`);
}

export function getRecords(novelId: string, chapter: number): Promise<RecordsResponse> {
  return request(`/novels/${novelId}/chapter/${chapter}/rows`);
}
export function getRecordsInspector(novelId: string, chapter: number): Promise<RecordsInspectorResponse> {
  return request(`/novels/${novelId}/chapter/${chapter}/records/status`);
}
export function getWiki(novelId: string, at: number): Promise<WikiResponse> {
  return request(`/novels/${novelId}/wiki?at=${at}`);
}
export function retryRecords(novelId: string, chapter: number): Promise<{ status: string; run_id?: string }> {
  return request(`/novels/${novelId}/chapter/${chapter}/records/retry`, { method: "POST" });
}
export function retryRecordRendering(novelId: string, chapter: number): Promise<{ status: string }> {
  return request(`/novels/${novelId}/chapter/${chapter}/records/render-retry`, { method: "POST" });
}
export function rebuildRecords(novelId: string): Promise<{ generation_id: string; status: string }> {
  return request(`/novels/${novelId}/records/rebuild`, { method: "POST" });
}

export function listChapters(novelId: string, limit: number, offset: number): Promise<ChapterListResponse> {
  return request(`/novels/${novelId}/chapters?limit=${limit}&offset=${offset}`);
}

// Where this reader left off. 404s for a reader who has never opened this novel — callers
// treat that as "start at the beginning" rather than an error.
export function getProgress(novelId: string): Promise<Progress> {
  return request(`/novels/${novelId}/progress`);
}

export function getPipelineStatus(novelId: string): Promise<PipelineStatusResponse> {
  return request(`/novels/${novelId}/pipeline`);
}

export type QueueMode = "all" | "focused" | "paused";
export interface QueueControl {
  mode: QueueMode;
  focus_novel_id: string;
	mode_changed_at?: string;
	mode_changed_by?: string;
	mode_reason?: string;
  books: Array<{
    novel_id: string;
    title: string;
    pending: number;
    in_flight: Array<{ chapter_index: number; stage: string }>;
  }>;
}
export function getQueueControl(): Promise<QueueControl> {
  return request("/queue");
}
// Serialize navigation writes in this tab so a slow response from book A cannot
// override the newer selection of book B. Other tabs share the same library policy.
let queueUpdates: Promise<unknown> = Promise.resolve();
export function updateQueueControl(patch: { mode?: QueueMode; focus_novel_id?: string; reason?: string }): Promise<QueueControl> {
  const update = queueUpdates.then(() => request<QueueControl>("/queue", {
    method: "PATCH", body: JSON.stringify(patch),
  }));
  queueUpdates = update.catch(() => undefined);
  return update;
}

export function getEntity(novelId: string, entityId: string, at: number): Promise<EntityResponse> {
  return request(`/novels/${novelId}/entity/${entityId}?at=${at}`);
}

export function ask(novelId: string, question: string, at: number): Promise<AskResponse> {
  return request(`/novels/${novelId}/ask`, {
    method: "POST",
    body: JSON.stringify({ question, at }),
  });
}

export function startScrape(novelId: string, body: StartScrapeRequest): Promise<{ id: number }> {
  return request(`/novels/${novelId}/scrape`, { method: "POST", body: JSON.stringify(body) });
}

export function getScrapeStatus(novelId: string): Promise<ScrapeJobView> {
  return request(`/novels/${novelId}/scrape/status`);
}

export function cancelScrape(novelId: string): Promise<{ status: string }> {
  return request(`/novels/${novelId}/scrape/cancel`, { method: "POST" });
}

export function getGlossary(novelId: string, at?: number): Promise<GlossaryResponse> {
  return request(`/novels/${novelId}/glossary${at === undefined ? "" : `?at=${at}`}`);
}

export function getVocabulary(novelId: string): Promise<VocabularyResponse> {
  return request(`/novels/${novelId}/vocabulary`);
}

export function mutateVocabulary(novelId: string, body: VocabularyMutationRequest): Promise<unknown> {
  return request(`/novels/${novelId}/vocabulary`, { method: "PATCH", body: JSON.stringify(body) });
}

export function correctGlossaryTerm(
  novelId: string,
  sourceTerm: string,
  body: CorrectGlossaryTermRequest,
): Promise<CorrectGlossaryTermResponse> {
  return request(`/novels/${novelId}/glossary/${encodeURIComponent(sourceTerm)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function bootstrapGlossary(
  novelId: string,
  body: BootstrapGlossaryRequest,
): Promise<BootstrapGlossaryResponse> {
  return request(`/novels/${novelId}/glossary/bootstrap`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function confirmGlossaryTerm(
  novelId: string,
  body: { source_term: string; target_term: string; at_chapter: number; term_role: import("./types").TermRole },
): Promise<CorrectGlossaryTermResponse> {
  return request(`/novels/${novelId}/glossary/confirm`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getCharacterNameReviews(novelId: string, chapter?: number): Promise<CharacterNameReviewsResponse> {
  const query = new URLSearchParams({ status: "pending" });
  if (chapter !== undefined) query.set("chapter", String(chapter));
  return request(`/novels/${novelId}/name-reviews?${query}`);
}

export function approveCharacterName(novelId: string, sourceTerm: string, targetTerm: string, termRole: import("./types").TermRole): Promise<unknown> {
  return request(`/novels/${novelId}/name-reviews/${encodeURIComponent(sourceTerm)}/approve`, {
    method: "POST",
    body: JSON.stringify({ target_term: targetTerm, term_role: termRole }),
  });
}

// Queue a window of chapters for translation starting at `from`. Omitting `count` lets the
// server apply the novel's own translate_lookahead. Safe to call repeatedly: the server
// only queues chapters not already queued, so a repeat returns an empty list.
export function translateAhead(
  novelId: string,
  from: number,
  count?: number,
): Promise<{ novel_id: string; queued: number[] }> {
  return request(`/novels/${novelId}/translate-ahead`, {
    method: "POST",
    body: JSON.stringify(count === undefined ? { from } : { from, count }),
  });
}

// A chapter's translation as it is being produced. `available` is false whenever nothing
// is streaming — before TRANSLATE starts, once the chapter is finished, or on a provider
// that can't stream — so callers render on `available`, not on truthiness of `text`.
export function getChapterPreview(
  novelId: string,
  n: number,
): Promise<{
  novel_id: string;
  chapter_index: number;
  available: boolean;
  text?: string;
  // Pipeline status of the chapter itself, so one read answers both "how far along is it"
  // and "is it readable now".
  status: string;
  // Safe provider category derived by reader-api from the latest chapter failure.
  failure_category?: string;
}> {
  return request(`/novels/${novelId}/chapter/${n}/preview`);
}

export function getTranslationHealth(novelId: string): Promise<TranslationHealth> {
  return request(`/novels/${novelId}/translation-health`);
}

export async function getProviderHealth(
  novelId: string,
  track: import("./types").ProviderHealthTrack,
): Promise<import("./types").ProviderHealth> {
  try {
    return await request(`/novels/${novelId}/provider-health?track=${track}`);
  } catch (err) {
    // Health deliberately answers with a safe JSON body on non-2xx so the UI can render
    // the cause. Transport failures still throw and are shown as a probe-unavailable state.
    if (err instanceof ApiError && err.body && typeof err.body === "object" &&
      "category" in err.body && "provider" in err.body && "endpoint_kind" in err.body) {
      return err.body as import("./types").ProviderHealth;
    }
    throw err;
  }
}

export function putProgress(novelId: string, chapter: number): Promise<Progress> {
  return request(`/novels/${novelId}/progress`, {
    method: "PUT",
    body: JSON.stringify({ chapter }),
  });
}

export { ApiError };

export function deleteGlossaryTerm(novelId: string, sourceTerm: string, at: number): Promise<{ deleted: boolean }> {
  return request(`/novels/${novelId}/glossary/${encodeURIComponent(sourceTerm)}`, {
    method: "DELETE", body: JSON.stringify({ at_chapter: at }),
  });
}

export function prioritizeChapter(novelId: string, chapter: number): Promise<{ prioritized: boolean }> {
  return request(`/novels/${novelId}/translate-ahead`, {
    method: "POST", body: JSON.stringify({ from: chapter, count: 1, priority: true }),
  });
}

export function getTimeline(novelId: string, at?: number): Promise<TimelineResponse> {
  return request(`/novels/${novelId}/timeline${at === undefined ? "" : `?at=${at}`}`);
}


// null (not a thrown 404) when the novel has no provider_config row: "inheriting the
// server default" is an ordinary state for a novel, not an error the caller must catch.
export async function getProviderConfig(novelId: string): Promise<ProviderConfigView | null> {
  try {
    return await request<ProviderConfigView>(`/novels/${novelId}/provider-config`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export function saveProviderConfig(
  novelId: string,
  body: SaveProviderConfigRequest,
): Promise<ProviderConfigView> {
  return request(`/novels/${novelId}/provider-config`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function listOllamaModels(novelId: string, target: "configured" | "graph" = "configured"): Promise<string[]> {
  const query = target === "graph" ? "?target=graph" : "";
  const response = await request<{ models: string[] }>(`/novels/${novelId}/provider-config/ollama-models${query}`);
  return response.models;
}


// Global provider credentials. Ungated like the other administration routes; reads are
// masked server-side, so no key ever reaches the browser.
export function listProviderCredentials(): Promise<ProviderCredentialsResponse> {
  return request(`/provider-credentials`);
}

export function saveProviderCredential(
  provider: string,
  body: SaveProviderCredentialRequest,
): Promise<ProviderCredentialsResponse> {
  return request(`/provider-credentials/${provider}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// The only way to clear a stored key: an omitted key on save means "unchanged".
export async function deleteProviderCredential(provider: string): Promise<void> {
  await fetch(`/api/provider-credentials/${provider}`, {
    method: "DELETE",
    headers: { "X-Reader-ID": readerId() },
  });
}
