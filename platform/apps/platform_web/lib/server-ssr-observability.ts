import "server-only";

import { headers } from "next/headers";
import { cache } from "react";

type SsrTrace = {
  requestId: string;
  cfRay: string;
  sampled: boolean;
};

const enabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
const sampleRate = boundedNumber(
  process.env.PLATFORM_SSR_PERF_SAMPLE_RATE,
  0.01,
  0,
  1
);

function boundedNumber(
  rawValue: string | undefined,
  fallback: number,
  minimum: number,
  maximum: number
): number {
  const value = Number(rawValue);
  return Number.isFinite(value)
    ? Math.min(maximum, Math.max(minimum, value))
    : fallback;
}

function safeToken(value: string | null | undefined, fallback: string): string {
  const normalized = value?.trim() || "";
  return /^[A-Za-z0-9._:-]{1,128}$/u.test(normalized) ? normalized : fallback;
}

function formatDuration(value: number): string {
  return Number.isFinite(value) ? value.toFixed(3) : "0.000";
}

// React's cache is request-scoped for Server Components. Keeping the trace
// separate from the auth cache lets layout and page timings share one safe
// request identifier without changing any cache key or auth behavior.
const getSsrTrace = cache(async (): Promise<SsrTrace | null> => {
  if (!enabled) {
    return null;
  }
  let requestHeaders: Awaited<ReturnType<typeof headers>> | null = null;
  try {
    requestHeaders = await headers();
  } catch {
    // Build-time and non-request invocations have no request headers. Keep the
    // diagnostic optional rather than making SSR depend on observability.
  }
  return {
    requestId: safeToken(requestHeaders?.get("x-request-id"), "unknown"),
    cfRay: safeToken(requestHeaders?.get("cf-ray"), "unknown"),
    sampled: Math.random() < sampleRate
  };
});

export async function recordSsrStage(
  stage: string,
  durationMs: number,
  outcome: "ok" | "error" = "ok"
): Promise<void> {
  const trace = await getSsrTrace();
  if (!trace?.sampled) {
    return;
  }
  const safeStage = safeToken(stage, "unknown");
  console.info(
    `ssr_perf request_id=${trace.requestId} cf_ray=${trace.cfRay}`
      + ` stage=${safeStage} duration_ms=${formatDuration(durationMs)}`
      + ` outcome=${outcome}`
  );
}

export async function measureSsrStage<T>(
  stage: string,
  operation: () => Promise<T>
): Promise<T> {
  if (!enabled) {
    return operation();
  }
  const startedAt = performance.now();
  let outcome: "ok" | "error" = "ok";
  try {
    return await operation();
  } catch (error) {
    outcome = "error";
    throw error;
  } finally {
    await recordSsrStage(stage, performance.now() - startedAt, outcome);
  }
}
