import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";
import { headers } from "next/headers";
import { cache } from "react";

type SsrTrace = {
  requestId: string;
  cfRay: string;
  sampled: boolean;
  startedAt: number;
  rootStartedAtMs: number;
  requestStartedAtMs: number | null;
  proxyStartedAtMs: number | null;
};

const enabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
const sampleRate = boundedNumber(
  process.env.PLATFORM_SSR_PERF_SAMPLE_RATE,
  0.01,
  0,
  1
);
const SSR_TRACE_HEADER = "x-platform-ssr-trace";
const SSR_PROXY_START_HEADER = "x-platform-ssr-proxy-start-ms";
const SSR_REQUEST_START_HEADER = "x-platform-ssr-request-start-ms";
type RequestHeaderSource = Pick<Headers, "get">;
const traceStorage = new AsyncLocalStorage<SsrTrace>();

export function isSsrDiagnosticsEnabled(): boolean {
  return enabled;
}

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

function epochMilliseconds(value: string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function createTrace(
  startedAt: number,
  rootStartedAtMs: number,
  requestHeaders: RequestHeaderSource
): SsrTrace {
  const traceMarker = requestHeaders.get(SSR_TRACE_HEADER);
  const sampled = traceMarker === "1"
    ? true
    : traceMarker === "0"
      ? false
      : Math.random() < sampleRate;
  return {
    requestId: safeToken(requestHeaders.get("x-request-id"), "unknown"),
    cfRay: safeToken(requestHeaders.get("cf-ray"), "unknown"),
    sampled,
    startedAt,
    rootStartedAtMs,
    requestStartedAtMs: epochMilliseconds(requestHeaders.get(SSR_REQUEST_START_HEADER)),
    proxyStartedAtMs: epochMilliseconds(requestHeaders.get(SSR_PROXY_START_HEADER))
  };
}

// React's cache is request-scoped for Server Components. The AsyncLocalStorage
// seed gives the root layout an accurate function-entry timestamp; the cache
// then lets descendant Server Components reuse the same safe trace if React
// resumes them outside that callback.
const getSsrTrace = cache(async (): Promise<SsrTrace | null> => {
  if (!enabled) {
    return null;
  }
  const inheritedTrace = traceStorage.getStore();
  if (inheritedTrace) {
    return inheritedTrace;
  }
  let requestHeaders: Awaited<ReturnType<typeof headers>> | null = null;
  try {
    requestHeaders = await headers();
  } catch {
    // Build-time and non-request invocations have no request headers. Keep the
    // diagnostic optional rather than making SSR depend on observability.
  }
  return createTrace(performance.now(), Date.now(), requestHeaders ?? new Headers());
});

export async function runWithSsrTrace<T>(
  startedAt: number,
  rootStartedAtMs: number,
  requestHeaders: RequestHeaderSource,
  operation: () => Promise<T>
): Promise<T> {
  if (!enabled || traceStorage.getStore()) {
    return operation();
  }
  const trace = createTrace(startedAt, rootStartedAtMs, requestHeaders);
  return traceStorage.run(trace, async () => {
    // Seed React's request-local cache while the trace context is available.
    await getSsrTrace();
    return operation();
  });
}

export async function getServerRequestCorrelationHeaders(): Promise<Headers> {
  let requestHeaders: Awaited<ReturnType<typeof headers>> | null = null;
  try {
    requestHeaders = await headers();
  } catch {
    // Build-time and non-request invocations have no request headers.
  }
  const correlationHeaders = new Headers();
  for (const name of ["x-request-id", "cf-ray"]) {
    const value = safeToken(requestHeaders?.get(name), "unknown");
    if (value !== "unknown") {
      correlationHeaders.set(name, value);
    }
  }
  return correlationHeaders;
}

function recordSsrSpan(
  trace: SsrTrace,
  stage: string,
  startMs: number,
  endMs: number,
  durationMs: number,
  outcome: "ok" | "error"
): void {
  if (!trace.sampled) {
    return;
  }
  const safeStage = safeToken(stage, "unknown");
  console.info(
    `ssr_perf request_id=${trace.requestId} cf_ray=${trace.cfRay}`
      + ` stage=${safeStage} start_ms=${formatDuration(startMs)}`
      + ` end_ms=${formatDuration(endMs)} duration_ms=${formatDuration(durationMs)}`
      + ` outcome=${outcome}`
  );
}

export async function recordSsrStage(
  stage: string,
  durationMs: number,
  outcome: "ok" | "error" = "ok"
): Promise<void> {
  if (!enabled) {
    return;
  }
  const trace = await getSsrTrace();
  if (!trace) {
    return;
  }
  const endMs = Math.max(0, performance.now() - trace.startedAt);
  const boundedDurationMs = Math.max(0, Number(durationMs) || 0);
  recordSsrSpan(
    trace,
    stage,
    Math.max(0, endMs - boundedDurationMs),
    endMs,
    boundedDurationMs,
    outcome
  );
}

export async function recordSsrPoint(
  stage: string,
  offsetMs?: number,
  outcome: "ok" | "error" = "ok"
): Promise<void> {
  if (!enabled) {
    return;
  }
  const trace = await getSsrTrace();
  if (!trace) {
    return;
  }
  const pointMs = offsetMs === undefined
    ? Math.max(0, performance.now() - trace.startedAt)
    : Math.max(0, offsetMs);
  recordSsrSpan(trace, stage, pointMs, pointMs, 0, outcome);
}

export async function recordSsrRequestTimeline(): Promise<void> {
  const trace = await getSsrTrace();
  if (!trace || !trace.sampled) {
    return;
  }
  const requestOffsetMs = trace.requestStartedAtMs === null
    ? null
    : trace.requestStartedAtMs - trace.rootStartedAtMs;
  const proxyOffsetMs = trace.proxyStartedAtMs === null
    ? null
    : trace.proxyStartedAtMs - trace.rootStartedAtMs;
  if (requestOffsetMs !== null) {
    recordSsrSpan(
      trace,
      "http_request_start",
      requestOffsetMs,
      requestOffsetMs,
      0,
      "ok"
    );
  }
  if (proxyOffsetMs !== null) {
    recordSsrSpan(
      trace,
      "proxy_start",
      proxyOffsetMs,
      proxyOffsetMs,
      0,
      "ok"
    );
    recordSsrSpan(
      trace,
      "proxy_to_root_layout_start",
      proxyOffsetMs,
      0,
      Math.max(0, -proxyOffsetMs),
      "ok"
    );
  }
  if (requestOffsetMs !== null && proxyOffsetMs !== null) {
    recordSsrSpan(
      trace,
      "request_to_proxy",
      requestOffsetMs,
      proxyOffsetMs,
      Math.max(0, proxyOffsetMs - requestOffsetMs),
      "ok"
    );
  }
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
