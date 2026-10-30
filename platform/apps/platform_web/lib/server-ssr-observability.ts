import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";
import { headers } from "next/headers";
import { cache } from "react";
import { formatWorkspaceApiErrorDiagnostic } from "@/lib/ssr-error-diagnostic";
import { isHtmlSsrCaptureEligible } from "@/lib/ssr-capture-sampling.mjs";
import {
  current_diagnostic_run_id,
  currentTypedHtmlSsrDiagnosticExpiryMs,
  isCurrentTypedHtmlSsrDiagnosticPlan,
  runWithSsrDiagnosticPlan,
} from "@/lib/performance-diagnostic-plan";
import type { ServerInternalReadAdmissionObserver } from "@/lib/server-internal-read-scheduler";

type SsrTrace = {
  requestId: string;
  cfRay: string;
  sampled: boolean;
  diagnosticRunId: string | null;
  typedDiagnosticPlan: boolean;
  diagnosticExpiresAtMs: number | null;
  startedAt: number;
  rootStartedAtMs: number;
  requestStartedAtMs: number | null;
  proxyStartedAtMs: number | null;
};

const enabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
const workspaceErrorDiagnosticEnabled =
  process.env.PLATFORM_SSR_WORKSPACE_ERROR_DIAGNOSTIC === "true";
const sampleRate = boundedNumber(
  process.env.PLATFORM_SSR_PERF_SAMPLE_RATE,
  0.01,
  0,
  1
);
const SSR_TRACE_HEADER = "x-platform-ssr-trace";
const SSR_REQUEST_ID_HEADER = "x-platform-ssr-request-id";
const SSR_CF_RAY_HEADER = "x-platform-ssr-cf-ray";
const SSR_PROXY_START_HEADER = "x-platform-ssr-proxy-start-ms";
const SSR_REQUEST_START_HEADER = "x-platform-ssr-request-start-ms";
type RequestHeaderSource = Pick<Headers, "get">;
const traceStorage = new AsyncLocalStorage<SsrTrace>();

export function isSsrObservabilityActive(): boolean {
  return enabled
    || current_diagnostic_run_id() !== null
    || isCurrentTypedHtmlSsrDiagnosticPlan()
    || traceStorage.getStore()?.typedDiagnosticPlan === true;
}

export async function getSsrAdmissionStageObserver(): Promise<
  ServerInternalReadAdmissionObserver | undefined
> {
  if (!isSsrObservabilityActive()) {
    return undefined;
  }
  const trace = await getSsrTrace();
  if (!trace?.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
    return undefined;
  }
  return (waitMs, outcome) => recordSsrStage(
    "internal_read_admission_wait",
    waitMs,
    outcome,
  );
}

export async function getSsrAuthBodyParseObserver(): Promise<
  ServerInternalReadAdmissionObserver | undefined
> {
  if (!isSsrObservabilityActive()) {
    return undefined;
  }
  const trace = await getSsrTrace();
  if (!trace?.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
    return undefined;
  }
  return (durationMs, outcome) => recordSsrStage(
    "auth_body_parse",
    durationMs,
    outcome,
  );
}

export async function isSsrTraceSampled(): Promise<boolean> {
  const trace = await getSsrTrace();
  return Boolean(trace?.sampled
    && (trace.diagnosticExpiresAtMs === null || Date.now() <= trace.diagnosticExpiresAtMs));
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
  const diagnosticRunId = current_diagnostic_run_id();
  const typedDiagnosticPlan = isCurrentTypedHtmlSsrDiagnosticPlan();
  const requestId = safeToken(requestHeaders.get("x-request-id"), "unknown");
  const traceMarker = requestHeaders.get(SSR_TRACE_HEADER);
  const sampled = diagnosticRunId !== null
    ? typedDiagnosticPlan
      ? isHtmlSsrCaptureEligible(diagnosticRunId, requestId)
      : true
    : traceMarker === "1"
    ? true
    : traceMarker === "0"
      ? false
      : Math.random() < sampleRate;
  return {
    // Nginx owns request_id. Public timeout metadata never enters this trace.
    requestId,
    cfRay: safeToken(requestHeaders.get("cf-ray"), "unknown"),
    sampled,
    diagnosticRunId,
    typedDiagnosticPlan,
    diagnosticExpiresAtMs: currentTypedHtmlSsrDiagnosticExpiryMs(),
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
  const inheritedTrace = traceStorage.getStore();
  if (inheritedTrace) {
    return inheritedTrace;
  }
  if (!isSsrObservabilityActive()) {
    return null;
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
  requestHeaders: RequestHeaderSource,
  operation: () => Promise<T>
): Promise<T> {
  if (traceStorage.getStore()) {
    return operation();
  }
  const hasDiagnosticCandidate = /^[0-9a-f]{32}$/u.test(
    requestHeaders.get("x-platform-ssr-trace") ?? "",
  );
  if (!enabled && !hasDiagnosticCandidate) {
    return operation();
  }
  return runWithSsrDiagnosticPlan(requestHeaders, async () => {
    if (!isSsrObservabilityActive()) {
      return operation();
    }
    // Start clocks only after the server has activated observability or
    // validated the private diagnostic plan. Ordinary requests stay unmeasured.
    const typedPlan = isCurrentTypedHtmlSsrDiagnosticPlan();
    const diagnosticRunId = current_diagnostic_run_id();
    const requestId = safeToken(requestHeaders.get("x-request-id"), "unknown");
    const captureSelected = typedPlan
      && diagnosticRunId !== null
      && isHtmlSsrCaptureEligible(diagnosticRunId, requestId);
    const startedAt = typedPlan && !captureSelected ? 0 : performance.now();
    const rootStartedAtMs = typedPlan && !captureSelected ? 0 : Date.now();
    const trace = createTrace(startedAt, rootStartedAtMs, requestHeaders);
    return traceStorage.run(trace, async () => {
      // Seed React's request-local cache while the trace context is available.
      await getSsrTrace();
      return operation();
    });
  });
}

export async function getServerRequestCorrelationHeaders(): Promise<Headers> {
  const trace = await getSsrTrace();
  if (!trace
      || (!trace.sampled && !trace.typedDiagnosticPlan)
      || (trace.typedDiagnosticPlan && trace.diagnosticRunId === null)
      || (trace.typedDiagnosticPlan && trace.diagnosticExpiresAtMs === null)
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
    return new Headers();
  }

  // Use Nginx's trace identity directly so direct API fetches and SSR records
  // join on the same request ID, even if headers() is resolved separately.
  const correlationHeaders = new Headers();
  for (const [name, value] of [
    [SSR_REQUEST_ID_HEADER, trace.requestId],
    [SSR_CF_RAY_HEADER, trace.cfRay],
    ["x-request-id", trace.requestId],
    ["cf-ray", trace.cfRay]
  ] as const) {
    if (value !== "unknown") {
      correlationHeaders.set(name, value);
    }
  }
  correlationHeaders.set(SSR_TRACE_HEADER, trace.diagnosticRunId ?? "1");
  if (trace.typedDiagnosticPlan
      && trace.diagnosticRunId !== null
      && trace.requestStartedAtMs !== null) {
    correlationHeaders.set(SSR_REQUEST_START_HEADER, String(trace.requestStartedAtMs));
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
  if (!trace.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
    return;
  }
  const safeStage = safeToken(stage, "unknown");
  console.info(
    `ssr_perf request_id=${trace.requestId} cf_ray=${trace.cfRay}`
      + (trace.diagnosticRunId === null ? "" : ` diagnostic_run_id=${trace.diagnosticRunId}`)
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
  if (!isSsrObservabilityActive()) {
    return;
  }
  const trace = await getSsrTrace();
  if (!trace) {
    return;
  }
  if (!trace.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) return;
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
  if (!isSsrObservabilityActive()) {
    return;
  }
  const trace = await getSsrTrace();
  if (!trace) {
    return;
  }
  if (!trace.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) return;
  const pointMs = offsetMs === undefined
    ? Math.max(0, performance.now() - trace.startedAt)
    : Math.max(0, offsetMs);
  recordSsrSpan(trace, stage, pointMs, pointMs, 0, outcome);
}

export async function recordSsrWorkspaceApiError(status: number): Promise<void> {
  const diagnosticRunId = current_diagnostic_run_id();
  if (diagnosticRunId === null
      && traceStorage.getStore()?.typedDiagnosticPlan !== true
      && (!enabled || !workspaceErrorDiagnosticEnabled)) {
    return;
  }
  const trace = await getSsrTrace();
  if (!trace?.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
    return;
  }
  const record = formatWorkspaceApiErrorDiagnostic({
    requestId: trace.requestId,
    cfRay: trace.cfRay,
    status,
    diagnosticRunId: trace.diagnosticRunId
  });
  if (record !== null) {
    console.info(record);
  }
}

export async function recordSsrRequestTimeline(): Promise<void> {
  const trace = await getSsrTrace();
  if (!trace || !trace.sampled
      || (trace.diagnosticExpiresAtMs !== null && Date.now() > trace.diagnosticExpiresAtMs)) {
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
  if (!isSsrObservabilityActive()) {
    return operation();
  }
  if (traceStorage.getStore()?.sampled === false) {
    return operation();
  }
  const trace = traceStorage.getStore();
  if (trace?.diagnosticExpiresAtMs !== null
      && trace?.diagnosticExpiresAtMs !== undefined
      && Date.now() > trace.diagnosticExpiresAtMs) {
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
