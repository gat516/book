import { useCallback, useEffect, useRef, useState } from "react";
import { getProviderHealth } from "../api";
import { PROVIDER_LABELS } from "../providers";
import type { ProviderHealth as ProviderHealthData, ProviderHealthTrack } from "../types";

export const PROVIDER_FAILURE_DETAILS: Record<string, string> = {
  ok: "the provider is serving requests",
  unreachable: "the provider endpoint could not be reached",
  timeout: "the provider did not answer before the request timed out",
  credential_missing: "no API credential is configured for this provider",
  credential_rejected: "the provider rejected the API credential",
  rate_limited: "the provider is rate limiting requests; it will be tried again",
  quota_exhausted: "the provider quota is exhausted; wait for its reset or choose another provider",
  provider_retry_exhausted: "the provider rejected five consecutive attempts; automatic retries stopped",
  model_not_available: "the provider could not find the pinned model",
  model_server_error: "the provider returned a server error",
  unknown: "the provider health check could not identify the cause",
};

export const PROVIDER_HEALTH_DETAILS = PROVIDER_FAILURE_DETAILS as Record<ProviderHealthData["category"], string>;

export function providerFailureDetail(category: string | undefined): string | null {
  return category ? (PROVIDER_FAILURE_DETAILS[category] ?? PROVIDER_FAILURE_DETAILS.unknown) : null;
}

interface Props {
  novelId: string;
  track: ProviderHealthTrack;
  enabled?: boolean;
  compact?: boolean;
}

export function ProviderHealth({ novelId, track, enabled = true, compact = false }: Props) {
  const [health, setHealth] = useState<ProviderHealthData | null>(null);
  const [checking, setChecking] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const inFlight = useRef(false);
  const endpointKind = useRef<ProviderHealthData["endpoint_kind"] | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const poll = useCallback(async () => {
    if (!enabled || inFlight.current) return null;
    inFlight.current = true;
    setChecking(true);
    try {
      const result = await getProviderHealth(novelId, track);
      endpointKind.current = result.endpoint_kind;
      setHealth(result);
      setUnavailable(false);
      return result;
    } catch {
      setUnavailable(true);
      return null;
    } finally {
      inFlight.current = false;
      setChecking(false);
    }
  }, [enabled, novelId, track]);

  useEffect(() => {
    if (!enabled) {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      return;
    }
    endpointKind.current = null;
    let cancelled = false;
    const schedule = () => {
      if (cancelled) return;
      const interval = endpointKind.current === "hosted" ? 15_000 : 2_000;
      timer.current = setTimeout(async () => {
        await poll();
        schedule();
      }, interval);
    };
    void poll().then(schedule);
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [enabled, poll]);

  if (!enabled) return null;
  const label = health ? PROVIDER_LABELS[health.provider as keyof typeof PROVIDER_LABELS] ?? health.provider : "Provider";
  const detail = health ? PROVIDER_HEALTH_DETAILS[health.category] : unavailable ? "the provider health check is unavailable" : "checking the provider";
  return (
    <p className={compact ? "provider-health provider-health-compact" : "provider-health"} role={unavailable ? "status" : "status"}>
      <strong>{label}</strong>{" — "}{checking && !health ? "Checking…" : health?.category === "ok" ? "Connected" : detail}
    </p>
  );
}
