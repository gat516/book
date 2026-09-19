import type { RecordsStatus } from "./types";

export function retryCategoryLabel(category?: string | null): string {
  if (!category) return "a processing error";
  const labels: Record<string, string> = {
    credential_missing: "a missing provider key",
    credential_rejected: "a rejected provider key",
    model_not_available: "an unavailable model",
    model_server_error: "a model service error",
    provider_http_429: "the provider’s rate limit",
    provider_retry_exhausted: "repeated provider errors",
    quota_exhausted: "the provider’s usage limit",
    rate_limited: "the provider’s rate limit",
    unreachable: "an unreachable model service",
    provider_invalid_json: "an unreadable AI response",
    invalid_stage_output: "an AI response that failed validation",
    output_truncated: "an incomplete AI response",
    output_limit: "the model’s response length limit",
    provider_content_filtered: "a response blocked by the AI provider",
    unsupported_schema: "a model that does not support the required response format",
    prompt_too_large: "a request that exceeds the model’s input limit",
    provider_bad_request: "a request rejected by the AI provider",
    provider_timeout: "an AI request that timed out",
    timeout: "an AI request that timed out",
    provider_connection: "a connection failure to the AI provider",
    model_unreachable: "an unreachable model service",
    model_not_installed: "a model that is not installed",
    model_changed: "changed model settings",
    untranslated_output: "a translation left partly in Chinese",
  };
  return labels[category] ?? "a processing error";
}

export function failureExplanation(status: Pick<RecordsStatus, "retry_category" | "failure_detail">): string {
  const rawCategory = status.retry_category ?? status.failure_detail;
  const aliases: Record<string, string> = {
    provider_http_400: "provider_bad_request", provider_http_422: "provider_bad_request",
    provider_http_401: "credential_rejected", provider_http_403: "credential_rejected",
    provider_http_404: "model_not_available", provider_http_413: "prompt_too_large",
  };
  const category = aliases[rawCategory ?? ""] ?? rawCategory;
  const messages: Record<string, string> = {
    provider_invalid_json: "The AI returned a response that could not be read",
    invalid_stage_output: "The AI response did not match the required format",
    output_truncated: "The AI stopped before completing its response",
    output_limit: "The AI reached its response length limit before finishing",
    provider_content_filtered: "The AI provider blocked the response with its content filter",
    unsupported_schema: "This model does not support the required response format",
    prompt_too_large: "The request exceeds this model’s input limit",
    provider_bad_request: "The AI provider rejected the request",
    credential_missing: "The AI provider needs an API key",
    credential_rejected: "The AI provider rejected the API key",
    model_not_available: "The selected AI model is unavailable",
    model_not_installed: "The selected model is not installed on the model server",
    model_changed: "The model settings changed during processing",
    untranslated_output: "The translation left part of the chapter in Chinese, even after a retry",
    provider_timeout: "The AI request timed out",
    timeout: "The AI request timed out",
    provider_connection: "The connection to the AI provider failed",
    unreachable: "The AI service could not be reached",
    model_unreachable: "The AI service could not be reached",
    model_server_error: "The AI provider reported a server error",
    rate_limited: "The AI provider is temporarily limiting requests",
    provider_http_429: "The AI provider is temporarily limiting requests",
    quota_exhausted: "The AI provider’s usage allowance has been exhausted",
    provider_retry_exhausted: "The AI provider repeatedly failed to accept the request",
    provider_batch_failed: "An AI request failed",
  };
  return `${messages[category ?? ""] ?? "An unexpected processing error occurred"}.`;
}

export function scheduledRetryMessage(status: RecordsStatus): string {
  const attempts = status.retry_attempts ?? 0;
  const max = status.retry_max_attempts ?? 0;
  const count = max > 0 ? `${attempts} of ${max} attempts failed` : `${attempts} attempts failed`;
  return `${failureExplanation(status)} ${count}. Next automatic retry: ${retryTimeLabel(status.retry_at)}.`;
}

export function retryTimeLabel(value?: string | null): string {
  if (!value) return "soon";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function retriesExhausted(status: RecordsStatus): boolean {
  return status.extraction_status === "failed" &&
    !status.retry_at &&
    (status.retry_max_attempts ?? 0) > 0 &&
    (status.retry_attempts ?? 0) >= (status.retry_max_attempts ?? 0);
}

// One colour language across status surfaces: green is work in flight, amber is waiting
// on something, red is a failure, grey is settled.
export type StatusTone = "live" | "warn" | "bad" | "quiet";

export interface ChapterStatusState {
  tone: StatusTone;
  text: string;
  // Only states a reader can act on offer Retry: a scheduled retry will happen by itself.
  retryable: boolean;
}

/**
 * The one-line status of a chapter's names and facts. A chapter is done when FACTS has
 * written its facts (migration 0110); name highlighting runs just before it in the same
 * pass, so "ready" covers both.
 */
export function chapterStatusState(status: RecordsStatus | null): ChapterStatusState {
  if (status === null) return { tone: "quiet", text: "Checking…", retryable: false };
  if (status.discarded) return { tone: "quiet", text: "Paused", retryable: true };
  if (status.retry_at) return { tone: "warn", text: scheduledRetryMessage(status), retryable: false };
  if (retriesExhausted(status) || status.extraction_status === "failed") {
    return { tone: "bad", text: `${failureExplanation(status)}.`, retryable: true };
  }
  if (status.extraction_status === "processing") {
    return { tone: "live", text: "Finding names and facts…", retryable: false };
  }
  if (status.extraction_status !== "ready") {
    return { tone: "live", text: "Waiting to find names and facts…", retryable: false };
  }
  const facts = status.facts_count;
  return {
    tone: "quiet",
    text: facts == null ? "Ready" : `Ready · ${facts} fact${facts === 1 ? "" : "s"} for the wiki`,
    retryable: false,
  };
}
