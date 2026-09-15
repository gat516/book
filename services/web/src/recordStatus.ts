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
  };
  return labels[category] ?? "a processing error";
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
