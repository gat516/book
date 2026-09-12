import type { RecordsStatus } from "./types";

export function retryCategoryLabel(category?: string | null): string {
  if (!category) return "record processing";
  return category.replaceAll("_", " ");
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
