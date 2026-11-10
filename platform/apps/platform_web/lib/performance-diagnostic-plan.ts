import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";
import { constants as fsConstants, openSync, closeSync, fstatSync, lstatSync, readFileSync, readSync } from "node:fs";
import { availableParallelism } from "node:os";
import { constants as performanceConstants, PerformanceObserver } from "node:perf_hooks";
import inspector from "node:inspector";
import { getHeapStatistics } from "node:v8";
import { headers } from "next/headers";
import { hasAuthorizedDiagnosticRunMarker } from "./performance-diagnostic-marker";
import type { CpuProfileFrame } from "./cpu-profile-source-maps.mjs";
import {
  CPU_PROFILE_SAMPLING,
  categoryForFrame,
  summarizeSampleClasses,
  summarizeFunctionSamples,
} from "./cpu-profile-categories.mjs";
import { mapSampledFrames } from "./cpu-profile-source-maps.mjs";
import { mapAfterCpuWindowSnapshot, runCpuWindowSetup } from "./cpu-profile-window.mjs";
import {
  isHtmlSsrCaptureEligible,
  isHtmlSsrTraceDrainEligible,
} from "./ssr-capture-sampling.mjs";
import { releaseMetadataPathFromStandaloneCwd } from "./release-identity-path.mjs";
import {
  claimWorkspaceCpuWindowStart,
  emitSsrWebLifecycleEvent,
  scheduleAuthorizedWorkspaceCpuWindowEdges,
  scheduleWorkspaceCpuProfilePrearm,
  scheduleWorkspaceCpuWindowStart,
} from "./workspace-cpu-window-scheduler.mjs";
import {
  authorizedWorkspaceDiagnosticPrearmRunId,
  readAuthorizedWorkspaceDiagnosticPrearmRunId,
} from "./workspace-diagnostic-prearm.mjs";
import {
  appendBoundedSample,
  classifyCgroupReadFailure,
  formatCpuDiagnosticMemoryEvent,
  isStableBoundedPseudoFileRead,
  timelineEntryDurationWithinWindow,
} from "./server-ssr-heap-metrics.mjs";

const PLAN_DIRECTORY = "/run/oldsparky-platform";
const PLAN_PATH = `${PLAN_DIRECTORY}/performance-diagnostic-plan.web.json`;
const MAX_PLAN_BYTES = 4096;
const MAX_TOTAL_WINDOW_MS = 160_000;
const OFF_WINDOW_MS = 20_000;
const IDLE_WINDOW_MS = 5_000;
const ON_WINDOW_MS = 20_000;
const HTML_CAPACITY_ON_WINDOW_MS = 120_000;
const MAX_SAMPLES = CPU_PROFILE_SAMPLING.maxSamples;
const MAX_PROFILE_NODES = CPU_PROFILE_SAMPLING.maxNodes;
const MAX_PROFILE_CPU_US = 60_000_000;
const CPU_WINDOW_TOLERANCE_MS = 250;
const MEMORY_SAMPLE_INTERVAL_MS = 4_000;
const MEMORY_SAMPLE_LIMIT = 5;
const GC_EVENT_LIMIT = 10_000;
const CGROUP_MEMORY_EVENT_NAMES = ["low", "high", "max", "oom", "oom_kill", "oom_group_kill"] as const;
const WORKLOAD_CPU_PAIR = "authenticated_workspace_read_pair_v1";
const WORKLOAD_SSR_CAPACITY = "authenticated_workspace_read_capacity_v1";
const WORKLOAD_HTML_SSR_CAPACITY = "authenticated_html_ssr_capacity_v1";
const WORKLOAD_HTML_SSR_CAPACITY_120S = "authenticated_html_ssr_capacity_120s_v1";
const WORKLOAD_HTML_SSR_CPU_PROFILE_20S = "authenticated_html_ssr_cpu_profile_20s_v1";
const DIAGNOSTIC_TRACE_HEADER = "x-platform-ssr-trace";
const SSR_REQUEST_START_HEADER = "x-platform-ssr-request-start-ms";
const SSR_STREAM_SELECTOR = Symbol.for("old-sparky.ssr-stream-selector");

type PlanTarget = {
  readonly service: "web";
  readonly pid: number;
  readonly start_ticks: number;
  readonly invocation_id: string;
};

type DiagnosticPlan = {
  readonly schema: 1;
  readonly run_id: string;
  readonly source_sha: string;
  readonly release_slug: string;
  readonly workload: typeof WORKLOAD_CPU_PAIR | typeof WORKLOAD_SSR_CAPACITY
    | typeof WORKLOAD_HTML_SSR_CAPACITY | typeof WORKLOAD_HTML_SSR_CAPACITY_120S
    | typeof WORKLOAD_HTML_SSR_CPU_PROFILE_20S;
  readonly off_start_ms: number;
  readonly off_end_ms: number;
  readonly on_start_ms: number;
  readonly on_end_ms: number;
  readonly expires_at_ms: number;
  readonly targets: readonly PlanTarget[];
};

export type DiagnosticPhase = "off" | "on";
type CpuUsageWindowSnapshot = { readonly promise: Promise<void>; readonly resolve: () => void };
export type WorkspaceCpuDiagnostic = {
  readonly runId: string;
  readonly phase: DiagnosticPhase;
  readonly active: true;
};

type ActiveDiagnostic = {
  readonly plan: DiagnosticPlan;
  readonly phase: DiagnosticPhase;
  readonly traceOnly?: true;
  readonly parentStartMs?: number;
};

const diagnosticStorage = new AsyncLocalStorage<ActiveDiagnostic>();
// Prearm only carries an authorized marker to the workspace API fetch. It must
// not make current_diagnostic_run_id() or generic SSR telemetry active.
const diagnosticPrearmStorage = new AsyncLocalStorage<DiagnosticPlan>();
const consumedCpuRunIds = new Set<string>();
const webCpuProfileScheduled = new Set<string>();
const webCpuProfileStarted = new Set<string>();
const webCpuProfileFailed = new Set<string>();
const usageWindowsStarted = new Set<string>();
const usageWindowsScheduled = new Set<string>();
const usageWindowsFailed = new Set<string>();
const cpuUsageWindowSnapshots = new Map<string, CpuUsageWindowSnapshot>();
const memoryWindowsStarted = new Set<string>();
let activeCpuRunId: string | null = null;
let planCache: { checkedAt: number; plan: DiagnosticPlan | null } | null = null;
const PLAN_CACHE_MS = 100;

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`
    ).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}
function readBounded(fd: number, limit: number): Buffer {
  const buffer = Buffer.alloc(limit + 1);
  const bytes = readSync(fd, buffer, 0, limit + 1, 0);
  return buffer.subarray(0, bytes);
}

function safeReadPlan(): DiagnosticPlan | null {
  let fd: number | null = null;
  try {
    const directory = lstatSync(PLAN_DIRECTORY);
    if (!directory.isDirectory() || directory.isSymbolicLink()
        || directory.uid !== 0 || directory.gid !== 0
        || (directory.mode & 0o777) !== 0o711) {
      return null;
    }
    if (typeof process.getgid !== "function") {
      return null;
    }
    fd = openSync(PLAN_PATH, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const before = fstatSync(fd);
    if (!before.isFile() || before.uid !== 0 || before.gid !== process.getgid()
        || (before.mode & 0o777) !== 0o440 || before.nlink !== 1
        || before.size <= 0 || before.size > MAX_PLAN_BYTES) {
      return null;
    }
    const raw = readBounded(fd, MAX_PLAN_BYTES);
    const after = fstatSync(fd);
    const pathAfter = lstatSync(PLAN_PATH);
    if (raw.length !== before.size || before.dev !== after.dev || before.ino !== after.ino
        || before.size !== after.size || pathAfter.isSymbolicLink()
        || pathAfter.dev !== before.dev || pathAfter.ino !== before.ino) {
      return null;
    }
    const text = raw.toString("utf8");
    const payload: unknown = JSON.parse(text);
    if (text !== `${canonicalJson(payload)}\n`) {
      return null;
    }
    return validatePayload(payload);
  } catch {
    return null;
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

function validatePayload(value: unknown): DiagnosticPlan | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const plan = value as Record<string, unknown>;
  const keys = [
    "schema", "run_id", "source_sha", "release_slug", "workload",
    "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms",
    "expires_at_ms", "targets"
  ].sort();
  if (Object.keys(plan).sort().join("|") !== keys.join("|")) return null;
  if (plan.schema !== 1
      || (plan.workload !== WORKLOAD_CPU_PAIR
        && plan.workload !== WORKLOAD_SSR_CAPACITY
        && plan.workload !== WORKLOAD_HTML_SSR_CAPACITY
        && plan.workload !== WORKLOAD_HTML_SSR_CAPACITY_120S
        && plan.workload !== WORKLOAD_HTML_SSR_CPU_PROFILE_20S)
      || typeof plan.run_id !== "string" || !/^[0-9a-f]{32}$/u.test(plan.run_id)
      || typeof plan.source_sha !== "string" || !/^[0-9a-f]{40}$/u.test(plan.source_sha)
      || typeof plan.release_slug !== "string" || !/^[a-z0-9][a-z0-9-]{0,79}$/u.test(plan.release_slug)) {
    return null;
  }
  const timeNames = ["off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"] as const;
  const times = timeNames.map((name) => plan[name]);
  if (times.some((time) => typeof time !== "number" || !Number.isSafeInteger(time) || time <= 0)) return null;
  const [offStart, offEnd, onStart, onEnd, expires] = times as number[];
  const expectedOnWindow = plan.workload === WORKLOAD_HTML_SSR_CAPACITY_120S
    ? HTML_CAPACITY_ON_WINDOW_MS
    : ON_WINDOW_MS;
  const typedHtmlWorkload = plan.workload === WORKLOAD_HTML_SSR_CAPACITY_120S
    || plan.workload === WORKLOAD_HTML_SSR_CAPACITY
    || plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S;
  const expectedExpiry = onEnd + (typedHtmlWorkload ? 15_000 : 0);
  if (!(offStart < offEnd && offEnd <= onStart && onStart < onEnd && onEnd <= expires)
      || offEnd - offStart !== OFF_WINDOW_MS
      || onStart - offEnd !== IDLE_WINDOW_MS
      || onEnd - onStart !== expectedOnWindow
      || expires !== expectedExpiry
      || expires - offStart > MAX_TOTAL_WINDOW_MS || onEnd - onStart > MAX_TOTAL_WINDOW_MS) return null;
  if (!Array.isArray(plan.targets) || plan.targets.length < 1 || plan.targets.length > 32) return null;
  const targetKeys = new Set<string>();
  for (const candidate of plan.targets) {
    if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) return null;
    const target = candidate as Record<string, unknown>;
    if (Object.keys(target).sort().join("|") !== "invocation_id|pid|service|start_ticks"
        || target.service !== "web" || typeof target.pid !== "number"
        || !Number.isSafeInteger(target.pid) || target.pid <= 0
        || typeof target.start_ticks !== "number" || !Number.isSafeInteger(target.start_ticks)
        || target.start_ticks <= 0 || typeof target.invocation_id !== "string"
        || !/^(?:[0-9a-f]{32}|[0-9a-f-]{36})$/u.test(target.invocation_id)) return null;
    const identity = `${target.service}:${target.pid}:${target.start_ticks}:${target.invocation_id}`;
    if (targetKeys.has(identity)) return null;
    targetKeys.add(identity);
  }
  const releaseIdentity = currentReleaseIdentity();
  if (releaseIdentity === null || releaseIdentity.sourceSha !== plan.source_sha
      || releaseIdentity.slug !== plan.release_slug) return null;
  const startTicks = currentProcessStartTicks();
  const invocationId = process.env.INVOCATION_ID ?? "";
  if (process.env.PLATFORM_RUNTIME_SERVICE !== "web" || startTicks === null
      || !/^(?:[0-9a-f]{32}|[0-9a-f-]{36})$/u.test(invocationId)
      || !plan.targets.some((target) => target.service === "web"
        && target.pid === process.pid && target.start_ticks === startTicks
        && target.invocation_id === invocationId)) return null;
  return plan as unknown as DiagnosticPlan;
}

function currentProcessStartTicks(): number | null {
  let fd: number | null = null;
  try {
    fd = openSync("/proc/self/stat", fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const buffer = Buffer.alloc(4096);
    const bytes = readSync(fd, buffer, 0, buffer.length, 0);
    const raw = buffer.subarray(0, bytes).toString("ascii");
    const end = raw.lastIndexOf(")");
    const fields = raw.slice(end + 2).trim().split(/\s+/u);
    if (end < 0 || fields.length <= 19) return null;
    const ticks = Number(fields[19]);
    return Number.isSafeInteger(ticks) && ticks > 0 ? ticks : null;
  } catch {
    return null;
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

function currentReleaseIdentity(): { sourceSha: string; slug: string } | null {
  let fd: number | null = null;
  try {
    const releasePath = releaseMetadataPathFromStandaloneCwd(process.cwd());
    const info = lstatSync(releasePath);
    if (!info.isFile() || info.isSymbolicLink() || info.uid !== 0 || info.nlink !== 1
        || info.size <= 0 || info.size > 16_384) return null;
    fd = openSync(releasePath, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    let raw: Buffer;
    try {
      const opened = fstatSync(fd);
      if (!opened.isFile() || opened.uid !== 0 || opened.nlink !== 1
          || opened.dev !== info.dev || opened.ino !== info.ino || opened.size !== info.size) return null;
      raw = readBounded(fd, 16_384);
      const after = fstatSync(fd);
      if (raw.length !== info.size || after.dev !== info.dev || after.ino !== info.ino
          || after.size !== info.size) return null;
    } finally {
      closeSync(fd);
      fd = null;
    }
    const release = JSON.parse(raw.toString("utf8")) as Record<string, unknown>;
    if (typeof release.source_git_commit !== "string"
        || !/^[0-9a-f]{40}$/u.test(release.source_git_commit)
        || typeof release.release_slug !== "string"
        || !/^[a-z0-9][a-z0-9-]{0,79}$/u.test(release.release_slug)) return null;
    return { sourceSha: release.source_git_commit, slug: release.release_slug };
  } catch {
    return null;
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

function activePhase(plan: DiagnosticPlan, now: number): DiagnosticPhase | null {
  if (now >= plan.off_start_ms && now < plan.off_end_ms) return "off";
  if (now >= plan.on_start_ms && now < plan.on_end_ms) return "on";
  return null;
}

function currentPlan(): DiagnosticPlan | null {
  const now = performance.now();
  if (planCache !== null && now - planCache.checkedAt < PLAN_CACHE_MS) {
    return planCache.plan;
  }
  const plan = safeReadPlan();
  planCache = { checkedAt: now, plan };
  return plan;
}

export function current_diagnostic_run_id(): string | null {
  const current = diagnosticStorage.getStore();
  if (!current) return null;
  const now = Date.now();
  if (activePhase(current.plan, now) === current.phase) return current.plan.run_id;
  const parentStartMs = current.parentStartMs;
  return typeof parentStartMs === "number"
    && isHtmlSsrTraceDrainEligible(
      current.plan.workload,
      parentStartMs,
      now,
      current.plan.on_start_ms,
      current.plan.on_end_ms,
      current.plan.expires_at_ms,
    )
    ? current.plan.run_id
    : null;
}

export function isCurrentTypedHtmlSsrDiagnosticPlan(): boolean {
  const current = diagnosticStorage.getStore();
  return current !== undefined && (
    current.plan.workload === WORKLOAD_HTML_SSR_CAPACITY
    || current.plan.workload === WORKLOAD_HTML_SSR_CAPACITY_120S
    || current.plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S
  );
}

export function currentTypedHtmlSsrDiagnosticExpiryMs(): number | null {
  const current = diagnosticStorage.getStore();
  return current && (
    current.plan.workload === WORKLOAD_HTML_SSR_CAPACITY
    || current.plan.workload === WORKLOAD_HTML_SSR_CAPACITY_120S
    || current.plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S
  )
    ? current.plan.expires_at_ms
    : null;
}

function validatedSsrDiagnostic(requestHeaders: Pick<Headers, "get">): ActiveDiagnostic | null {
  const incomingTrace = requestHeaders.get(DIAGNOSTIC_TRACE_HEADER);
  if (!incomingTrace || !/^[0-9a-f]{32}$/u.test(incomingTrace)) return null;
  const plan = currentPlan();
  if (!plan
      || (plan.workload !== WORKLOAD_HTML_SSR_CAPACITY
        && plan.workload !== WORKLOAD_HTML_SSR_CAPACITY_120S
        && plan.workload !== WORKLOAD_HTML_SSR_CPU_PROFILE_20S)
      || !hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)) return null;
  const now = Date.now();
  const phase = activePhase(plan, now);
  const parentStartText = requestHeaders.get(SSR_REQUEST_START_HEADER) ?? "";
  const parentStartMs = /^(?:0|[1-9][0-9]{0,15})$/u.test(parentStartText)
    ? Number(parentStartText)
    : null;
  const traceEligible = parentStartMs !== null && isHtmlSsrTraceDrainEligible(
    plan.workload,
    parentStartMs,
    now,
    plan.on_start_ms,
    plan.on_end_ms,
    plan.expires_at_ms,
  );
  // Preserve ordinary OFF/ON telemetry and its capture starters. The broader
  // trace interval only supplies correlation context across phase edges.
  if (phase !== null) {
    return { plan, phase, ...(traceEligible ? { parentStartMs: parentStartMs! } : {}) };
  }
  if (!traceEligible) return null;
  return { plan, phase: "on", traceOnly: true, parentStartMs: parentStartMs! };
}

function registerSsrStreamRequest(
  requestHeaders: Pick<Headers, "get">,
  diagnostic: ActiveDiagnostic,
): void {
  const requestId = requestHeaders.get("x-request-id") ?? "";
  const register = (globalThis as typeof globalThis & Record<PropertyKey, unknown>)[SSR_STREAM_SELECTOR];
  if (!/^[0-9a-f]{32}$/u.test(requestId) || typeof register !== "function") return;
  try {
    (register as (entry: {
      requestId: string;
      runId: string;
      expiresAtMs: number;
      captureEligible: boolean;
    }) => unknown)({
      requestId,
      runId: diagnostic.plan.run_id,
      expiresAtMs: diagnostic.plan.expires_at_ms,
      captureEligible: isHtmlSsrCaptureEligible(diagnostic.plan.run_id, requestId),
    });
  } catch {
    // Stream diagnostics are optional; selection failure must not affect SSR.
  }
}

export async function runWithSsrDiagnosticPlan<T>(
  requestHeaders: Pick<Headers, "get">,
  operation: () => Promise<T>,
): Promise<T> {
  const inherited = diagnosticStorage.getStore();
  const diagnostic = inherited ?? validatedSsrDiagnostic(requestHeaders);
  if (!diagnostic) return operation();
  registerSsrStreamRequest(requestHeaders, diagnostic);
  if (diagnosticStorage.getStore()) return operation();
  if (diagnostic.traceOnly === true) {
    emitSsrWebLifecycleEvent(diagnostic.plan, "none", "mode", "trace_only");
    return diagnosticStorage.run(diagnostic, operation);
  }
  emitSsrWebLifecycleEvent(diagnostic.plan, diagnostic.phase, "mode", "active");
  startCpuUsageWindow(diagnostic.plan, diagnostic.phase);
  startNodeMemoryWindow(diagnostic.plan, diagnostic.phase);
  if (diagnostic.plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S) {
    scheduleWebCpuProfile(diagnostic.plan, diagnostic.phase);
  } else if (diagnostic.plan.workload === WORKLOAD_CPU_PAIR && diagnostic.phase === "on") {
    await startAuthorizedNodeCpuProfile(diagnostic.plan);
  }
  return diagnosticStorage.run(diagnostic, operation);
}

export function current_workspace_cpu_diagnostic_prearm_run_id(): string | null {
  const prearmPlan = diagnosticPrearmStorage.getStore();
  return readAuthorizedWorkspaceDiagnosticPrearmRunId(
    prearmPlan ?? null, safeReadPlan, Date.now(),
  );
}

export async function run_with_workspace_cpu_diagnostic<T>(
  operation: () => Promise<T>
): Promise<T> {
  const plan = currentPlan();
  if (!plan) return operation();
  const phase = activePhase(plan, Date.now());
  let incomingTrace: string | null = null;
  try {
    incomingTrace = (await headers()).get(DIAGNOSTIC_TRACE_HEADER);
  } catch {
    // A missing request context must never arm diagnostics.
    return operation();
  }
  if (!hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)) return operation();
  if (!phase) {
    const freshPlan = safeReadPlan();
    const prearmRunId = authorizedWorkspaceDiagnosticPrearmRunId(
      plan, freshPlan, Date.now(),
    );
    if (freshPlan !== null && scheduleAuthorizedWorkspaceCpuWindowEdges(
      plan,
      freshPlan,
      prearmRunId,
      (expected, current) => canonicalJson(expected) === canonicalJson(current),
      scheduleCpuUsageWindow,
    )) {
      if (freshPlan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S) {
        scheduleWebCpuProfile(freshPlan, null);
      }
      return diagnosticPrearmStorage.run(freshPlan, operation);
    }
    const reason = freshPlan === null ? "stale" : "mismatch";
    for (const skippedPhase of ["off", "on"] as const) {
      const targetMs = skippedPhase === "off" ? plan.off_start_ms : plan.on_start_ms;
      emitSsrWebLifecycleEvent(plan, skippedPhase, "cpu_window", "skipped", {
        reason,
        lagMs: Date.now() - targetMs,
      });
    }
    return operation();
  }
  emitSsrWebLifecycleEvent(plan, phase, "mode", "active");
  startCpuUsageWindow(plan, phase);
  startNodeMemoryWindow(plan, phase);
  if (plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S) {
    scheduleWebCpuProfile(plan, phase);
  } else if (plan.workload === WORKLOAD_CPU_PAIR && phase === "on") {
    await startAuthorizedNodeCpuProfile(plan);
  }
  return diagnosticStorage.run({ plan, phase }, operation);
}

function effectiveCpuCapacity(): number | null {
  let affinity: number;
  try {
    affinity = availableParallelism();
  } catch {
    return null;
  }
  if (!Number.isSafeInteger(affinity) || affinity < 1 || affinity > 4096) return null;
  try {
    const cgroup = readFileSync("/proc/self/cgroup", { encoding: "ascii" });
    if (cgroup.length > 8192) return null;
    const line = cgroup.split("\n").find((candidate) => candidate.startsWith("0::"));
    if (!line || !/^0::\/[A-Za-z0-9._/-]{0,512}$/u.test(line)) return null;
    const relative = line.slice(3).split("/").filter(Boolean);
    if (relative.some((part) => part === "." || part === "..")) return null;
    let capacity = affinity;
    const directories = [
      `/sys/fs/cgroup${relative.length ? `/${relative.join("/")}` : ""}`,
    ];
    while (directories[directories.length - 1] !== "/sys/fs/cgroup") {
      const current = directories[directories.length - 1];
      const parent = current.slice(0, current.lastIndexOf("/"));
      if (!parent || (parent !== "/sys/fs/cgroup" && !parent.startsWith("/sys/fs/cgroup/"))) return null;
      directories.push(parent);
    }
    for (const directory of directories) {
      let raw: string;
      try {
        raw = readFileSync(`${directory}/cpu.max`, { encoding: "ascii" });
      } catch (error) {
        if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
          continue;
        }
        return null;
      }
      if (raw.length > 128) return null;
      const fields = raw.trim().split(/\s+/u);
      if (fields.length !== 2 || !/^[1-9][0-9]{0,12}$/u.test(fields[1])) return null;
      const period = Number(fields[1]);
      if (!Number.isSafeInteger(period) || period <= 0) return null;
      if (fields[0] === "max") continue;
      if (!/^[1-9][0-9]{0,12}$/u.test(fields[0])) return null;
      const quotaCapacity = Number(fields[0]) / period;
      if (!Number.isFinite(quotaCapacity) || quotaCapacity <= 0 || quotaCapacity > 4096) return null;
      capacity = Math.min(capacity, quotaCapacity);
    }
    return capacity;
  } catch {
    return null;
  }
}

function scheduleCpuUsageWindow(plan: DiagnosticPlan, phase: DiagnosticPhase): void {
  scheduleWorkspaceCpuWindowStart(plan, phase, {
    now: Date.now,
    schedule: (callback, delayMs) => {
      const timer = setTimeout(callback, delayMs);
      timer.unref();
      return timer;
    },
    readPlan: safeReadPlan,
    samePlan: (expected, current) => current !== null
      && canonicalJson(expected) === canonicalJson(current),
    phaseAt: activePhase,
    startTarget: (current, currentPhase) => (
      currentPhase === "off" ? current.off_start_ms : current.on_start_ms
    ),
    expiresAt: (current) => current.expires_at_ms,
    started: usageWindowsStarted,
    scheduled: usageWindowsScheduled,
    failed: usageWindowsFailed,
    onStart: startCpuUsageWindow,
    onLifecycle: ({ status, reason, lagMs }) => {
      if (status !== "started") {
        emitSsrWebLifecycleEvent(plan, phase, "cpu_window", status, { reason, lagMs });
      }
    },
    startToleranceMs: CPU_WINDOW_TOLERANCE_MS,
  });
}

function startCpuUsageWindow(plan: DiagnosticPlan, phase: DiagnosticPhase): void {
  if (!claimWorkspaceCpuWindowStart(plan, phase, {
    started: usageWindowsStarted,
    scheduled: usageWindowsScheduled,
    failed: usageWindowsFailed,
  })) {
    const targetMs = phase === "off" ? plan.off_start_ms : plan.on_start_ms;
    emitSsrWebLifecycleEvent(plan, phase, "cpu_window", "skipped", {
      reason: "already_claimed", lagMs: Date.now() - targetMs,
    });
    return;
  }
  const snapshot = ensureCpuUsageWindowSnapshot(plan.run_id, phase);
  const key = cpuUsageWindowKey(plan.run_id, phase);
  runCpuWindowSetup(() => {
    if (cpuUsageWindowSnapshots.get(key) === snapshot) {
      cpuUsageWindowSnapshots.delete(key);
    }
    snapshot.resolve();
  }, () => {
    const startTarget = phase === "off" ? plan.off_start_ms : plan.on_start_ms;
    const endTarget = phase === "off" ? plan.off_end_ms : plan.on_end_ms;
    const expectedWindowMs = endTarget - startTarget;
    const startMs = Date.now();
    const startLagMs = startMs - startTarget;
    const startCpu = process.cpuUsage();
    const startMonotonic = performance.now();
    emitSsrWebLifecycleEvent(plan, phase, "cpu_window", "started", {
      lagMs: startLagMs,
    });
    const capacity = effectiveCpuCapacity();
    const timer = setTimeout(() => {
      let finalStatus: "finalized" | "failed" = "failed";
      let finalReason: "end_error" | "timing_incomplete" | "none" = "end_error";
      try {
        const endMs = Date.now();
        const endLagMs = endMs - endTarget;
        const windowMs = Math.max(0, Math.round(performance.now() - startMonotonic));
        const usage = process.cpuUsage(startCpu);
        const cpuNs = Math.max(0, usage.user + usage.system) * 1000;
        const timingComplete = Boolean(
          startLagMs >= 0 && startLagMs <= CPU_WINDOW_TOLERANCE_MS
          && Math.abs(endLagMs) <= CPU_WINDOW_TOLERANCE_MS
          && windowMs >= expectedWindowMs - CPU_WINDOW_TOLERANCE_MS
          && windowMs <= expectedWindowMs + 2 * CPU_WINDOW_TOLERANCE_MS
        );
        finalStatus = timingComplete ? "finalized" : "failed";
        finalReason = timingComplete ? "none" : "timing_incomplete";
        console.info(
          `cpu_diagnostic_usage service=web run_id=${plan.run_id} phase=${phase}`
          + ` window_ms=${windowMs} cpu_ns=${cpuNs}`
          + ` cpu_capacity_cpus=${capacity === null ? "unknown" : capacity.toFixed(6)}`
          + ` start_lag_ms=${startLagMs} end_lag_ms=${endLagMs}`
          + ` timing_complete=${String(timingComplete)}`
        );
      } catch (error) {
        finalStatus = "failed";
        finalReason = "end_error";
        throw error;
      } finally {
        emitSsrWebLifecycleEvent(plan, phase, "cpu_window", finalStatus, {
          reason: finalReason,
        });
        if (cpuUsageWindowSnapshots.get(key) === snapshot) {
          cpuUsageWindowSnapshots.delete(key);
        }
        snapshot.resolve();
      }
    }, Math.max(0, endTarget - startMs));
    timer.unref();
  });
}

function cpuUsageWindowKey(runId: string, phase: DiagnosticPhase): string {
  return `${runId}:${phase}`;
}

function ensureCpuUsageWindowSnapshot(
  runId: string,
  phase: DiagnosticPhase,
): CpuUsageWindowSnapshot {
  const key = cpuUsageWindowKey(runId, phase);
  const existing = cpuUsageWindowSnapshots.get(key);
  if (existing) return existing;
  let resolve!: () => void;
  const promise = new Promise<void>((complete) => { resolve = complete; });
  const snapshot = { promise, resolve };
  cpuUsageWindowSnapshots.set(key, snapshot);
  return snapshot;
}

async function waitForCpuUsageWindowSnapshot(
  runId: string,
  phase: DiagnosticPhase,
): Promise<void> {
  const key = cpuUsageWindowKey(runId, phase);
  if (usageWindowsFailed.has(key) && !usageWindowsStarted.has(key)) {
    cpuUsageWindowSnapshots.delete(key);
    return;
  }
  await cpuUsageWindowSnapshots.get(key)?.promise;
}

type CgroupMemorySnapshot = {
  readonly currentBytes: number;
  readonly peakBytes: number;
  readonly events: Readonly<Record<typeof CGROUP_MEMORY_EVENT_NAMES[number], number>>;
};

type CgroupMemoryRead =
  | { readonly status: "complete"; readonly reason: "none"; readonly snapshot: CgroupMemorySnapshot }
  | { readonly status: "unavailable"; readonly reason: "hierarchy_unavailable" | "file_unavailable"; readonly snapshot: null }
  | { readonly status: "invalid"; readonly reason: "invalid_snapshot"; readonly snapshot: null };

type NodeMemorySample = {
  readonly elapsedMs: number;
  readonly rssBytes: number;
  readonly heapTotalBytes: number;
  readonly heapUsedBytes: number;
  readonly heapLimitBytes: number;
  readonly externalBytes: number;
  readonly arrayBuffersBytes: number;
  readonly cgroupMemoryStatus: CgroupMemoryRead["status"];
  readonly cgroupMemoryReason: CgroupMemoryRead["reason"];
  readonly cgroup: CgroupMemorySnapshot | null;
};

type SafeCgroupText =
  | { readonly status: "complete"; readonly text: string }
  | { readonly status: "unavailable" | "invalid" };

function readSafeCgroupText(path: string, limit: number): SafeCgroupText {
  let fd: number | null = null;
  try {
    fd = openSync(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const info = fstatSync(fd);
    if (!info.isFile() || info.size < 0 || info.size > limit) return { status: "invalid" };
    const raw = readBounded(fd, limit);
    const after = fstatSync(fd);
    if (!isStableBoundedPseudoFileRead({
      beforeSize: info.size,
      afterSize: after.size,
      beforeDevice: info.dev,
      afterDevice: after.dev,
      beforeInode: info.ino,
      afterInode: after.ino,
      beforeRegularFile: info.isFile(),
      afterRegularFile: after.isFile(),
      byteLength: raw.length,
      limit,
    })) return { status: "invalid" };
    return { status: "complete", text: raw.toString("ascii") };
  } catch (error) {
    const code = error && typeof error === "object" && "code" in error ? error.code : null;
    return { status: classifyCgroupReadFailure(typeof code === "string" ? code : null) };
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

function readCgroupMemorySnapshot(): CgroupMemoryRead {
  const membership = readSafeCgroupText("/proc/self/cgroup", 8192);
  if (membership.status !== "complete") {
    if (membership.status === "unavailable") {
      return { status: "unavailable", reason: "hierarchy_unavailable", snapshot: null };
    }
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  const line = membership.text.split("\n").find((candidate) => candidate.startsWith("0::"));
  if (!line) return { status: "unavailable", reason: "hierarchy_unavailable", snapshot: null };
  if (!/^0::\/[A-Za-z0-9._/-]{0,512}$/u.test(line)) return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  const relative = line.slice(3).split("/").filter(Boolean);
  if (relative.some((part) => part === "." || part === "..")) return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  const directory = `/sys/fs/cgroup${relative.length ? `/${relative.join("/")}` : ""}`;
  const currentRead = readSafeCgroupText(`${directory}/memory.current`, 64);
  const peakRead = readSafeCgroupText(`${directory}/memory.peak`, 64);
  const eventsRaw = readSafeCgroupText(`${directory}/memory.events`, 512);
  if (currentRead.status === "invalid" || peakRead.status === "invalid" || eventsRaw.status === "invalid") {
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  if (currentRead.status === "unavailable" || peakRead.status === "unavailable" || eventsRaw.status === "unavailable") {
    return { status: "unavailable", reason: "file_unavailable", snapshot: null };
  }
  if (currentRead.status !== "complete" || peakRead.status !== "complete" || eventsRaw.status !== "complete") {
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  const currentRaw = currentRead.text.trim();
  const peakRaw = peakRead.text.trim();
  if (!currentRaw || !peakRaw || !/^[0-9]{1,16}$/u.test(currentRaw) || !/^[0-9]{1,16}$/u.test(peakRaw)) {
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  const events: Record<typeof CGROUP_MEMORY_EVENT_NAMES[number], number> = {
    low: -1, high: -1, max: -1, oom: -1, oom_kill: -1, oom_group_kill: -1,
  };
  const seen = new Set<string>();
  for (const row of eventsRaw.text.trim().split("\n")) {
    const fields = row.split(/\s+/u);
    if (fields.length !== 2 || !CGROUP_MEMORY_EVENT_NAMES.includes(fields[0] as typeof CGROUP_MEMORY_EVENT_NAMES[number])
        || !/^[0-9]{1,16}$/u.test(fields[1]) || seen.has(fields[0])) {
      return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
    }
    seen.add(fields[0]);
    events[fields[0] as typeof CGROUP_MEMORY_EVENT_NAMES[number]] = Number(fields[1]);
  }
  if (seen.size !== CGROUP_MEMORY_EVENT_NAMES.length
      || Object.values(events).some((value) => !Number.isSafeInteger(value) || value < 0)) {
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  const currentBytes = Number(currentRaw);
  const peakBytes = Number(peakRaw);
  if (!Number.isSafeInteger(currentBytes) || !Number.isSafeInteger(peakBytes)) {
    return { status: "invalid", reason: "invalid_snapshot", snapshot: null };
  }
  return { status: "complete", reason: "none", snapshot: { currentBytes, peakBytes, events } };
}

function captureNodeMemorySample(phaseStartMonotonicMs: number): NodeMemorySample | null {
  try {
    const memory = process.memoryUsage();
    const heap = getHeapStatistics();
    const values = [
      memory.rss, heap.total_heap_size, heap.used_heap_size, heap.heap_size_limit,
      memory.external, memory.arrayBuffers,
    ];
    if (values.some((value) => !Number.isSafeInteger(value) || value < 0)) return null;
    const cgroup = readCgroupMemorySnapshot();
    return {
      elapsedMs: Math.max(0, Math.round(performance.now() - phaseStartMonotonicMs)),
      rssBytes: memory.rss,
      heapTotalBytes: heap.total_heap_size,
      heapUsedBytes: heap.used_heap_size,
      heapLimitBytes: heap.heap_size_limit,
      externalBytes: memory.external,
      arrayBuffersBytes: memory.arrayBuffers,
      cgroupMemoryStatus: cgroup.status,
      cgroupMemoryReason: cgroup.reason,
      cgroup: cgroup.snapshot,
    };
  } catch {
    return null;
  }
}

function startNodeMemoryWindow(plan: DiagnosticPlan, phase: DiagnosticPhase): void {
  const key = `${plan.run_id}:${phase}`;
  if (memoryWindowsStarted.has(key)) {
    emitSsrWebLifecycleEvent(plan, phase, "memory", "skipped", { reason: "already_claimed" });
    return;
  }
  memoryWindowsStarted.add(key);
  emitSsrWebLifecycleEvent(plan, phase, "memory", "started");
  const samples: NodeMemorySample[] = [];
  let gcCount = 0;
  let gcDurationMs = 0;
  const gcCounts: Record<string, number> = { major: 0, minor: 0, incremental: 0, weakcb: 0, other: 0 };
  let gcOverflow = false;
  let observer: PerformanceObserver | null = null;
  const phaseStartEpochMs = phase === "off" ? plan.off_start_ms : plan.on_start_ms;
  const phaseDurationMs = phase === "off"
    ? plan.off_end_ms - plan.off_start_ms
    : plan.on_end_ms - plan.on_start_ms;
  const phaseStartMonotonicMs = performance.now() + (phaseStartEpochMs - Date.now());
  const phaseEndMonotonicMs = phaseStartMonotonicMs + phaseDurationMs;
  const consumeGcEntries = (entries: readonly PerformanceEntry[]): void => {
    for (const entry of entries) {
      const attributedDurationMs = timelineEntryDurationWithinWindow(
        entry.startTime, entry.duration, phaseStartMonotonicMs, phaseEndMonotonicMs,
      );
      if (attributedDurationMs === null) continue;
      if (gcCount >= GC_EVENT_LIMIT) {
        gcOverflow = true;
        continue;
      }
      gcCount += 1;
      gcDurationMs += attributedDurationMs;
      const detail = (entry as PerformanceEntry & { readonly detail?: unknown }).detail;
      const kind = detail && typeof detail === "object" && "kind" in detail
        ? Number(detail.kind) : Number.NaN;
      const category = kind === performanceConstants.NODE_PERFORMANCE_GC_MAJOR ? "major"
        : kind === performanceConstants.NODE_PERFORMANCE_GC_MINOR ? "minor"
          : kind === performanceConstants.NODE_PERFORMANCE_GC_INCREMENTAL ? "incremental"
            : kind === performanceConstants.NODE_PERFORMANCE_GC_WEAKCB ? "weakcb" : "other";
      gcCounts[category] += 1;
    }
  };
  try {
    observer = new PerformanceObserver((list) => consumeGcEntries(list.getEntries()));
    observer.observe({ entryTypes: ["gc"] });
  } catch {
    observer?.disconnect();
    observer = null;
  }

  const sample = (): void => {
    if (samples.length >= MEMORY_SAMPLE_LIMIT) return;
    const value = captureNodeMemorySample(phaseStartMonotonicMs);
    if (value !== null) appendBoundedSample(samples, value, MEMORY_SAMPLE_LIMIT);
  };
  sample();
  const sampleIntervalMs = phaseDurationMs > ON_WINDOW_MS
    ? Math.ceil(phaseDurationMs / (MEMORY_SAMPLE_LIMIT - 1))
    : MEMORY_SAMPLE_INTERVAL_MS;
  const interval = setInterval(sample, sampleIntervalMs);
  interval.unref();
  const finalizeMemoryWindow = (): void => {
    clearInterval(interval);
    sample();
    if (observer !== null) consumeGcEntries(observer.takeRecords());
    observer?.disconnect();
    const fields = ["rssBytes", "heapTotalBytes", "heapUsedBytes", "heapLimitBytes", "externalBytes", "arrayBuffersBytes"] as const;
    const aggregates = fields.map((field) => {
      const values = samples.map((item) => item[field]);
      const min = values.length ? Math.min(...values) : null;
      const max = values.length ? Math.max(...values) : null;
      const mean = values.length ? Math.round(values.reduce((total, value) => total + value, 0) / values.length) : null;
      const label = field.replace(/Bytes$/u, "").replace(/[A-Z]/gu, (letter) => `_${letter.toLowerCase()}`);
      return `${label}_min_bytes=${min ?? "unknown"} ${label}_max_bytes=${max ?? "unknown"} ${label}_mean_bytes=${mean ?? "unknown"}`;
    });
    const cgroupRows = samples.map((item) => item.cgroup).filter((item): item is CgroupMemorySnapshot => item !== null);
    const cgroupInvalid = samples.some((item) => item.cgroupMemoryStatus === "invalid");
    const cgroupMemoryStatus = cgroupInvalid ? "invalid"
      : cgroupRows.length === samples.length && samples.length === MEMORY_SAMPLE_LIMIT ? "complete" : "unavailable";
    const cgroupMemoryReason = cgroupMemoryStatus === "complete" ? "none"
      : cgroupInvalid ? "invalid_snapshot"
        : samples.some((item) => item.cgroupMemoryReason === "file_unavailable") ? "file_unavailable"
          : "hierarchy_unavailable";
    const first = cgroupRows[0];
    const last = cgroupRows.at(-1);
    const sampledWindowStartMs = samples[0]?.elapsedMs ?? null;
    const sampledWindowEndMs = samples.at(-1)?.elapsedMs ?? null;
    const sampledWindowDurationMs = sampledWindowStartMs !== null && sampledWindowEndMs !== null
      ? Math.max(0, sampledWindowEndMs - sampledWindowStartMs) : null;
    const cgroupMax = (field: "currentBytes" | "peakBytes"): number | null =>
      cgroupRows.length ? Math.max(...cgroupRows.map((item) => item[field])) : null;
    const eventDelta = (name: typeof CGROUP_MEMORY_EVENT_NAMES[number]): number | null =>
      first && last && last.events[name] >= first.events[name] ? last.events[name] - first.events[name] : null;
    const unknown = (value: number | null): string => value === null ? "unknown" : String(value);
    try {
      const tokens = [
        `service=web`, `run_id=${plan.run_id}`, `phase=${phase}`,
        `sample_count=${samples.length}`, `status=${samples.length === MEMORY_SAMPLE_LIMIT ? "complete" : "incomplete"}`,
        ...aggregates.flatMap((entry) => entry.split(" ")),
        `cgroup_sample_count=${cgroupRows.length}`,
        `cgroup_memory_status=${cgroupMemoryStatus}`, `cgroup_memory_reason=${cgroupMemoryReason}`,
        `sampled_window_start_elapsed_ms=${unknown(sampledWindowStartMs)}`,
        `sampled_window_end_elapsed_ms=${unknown(sampledWindowEndMs)}`,
        `sampled_window_duration_ms=${unknown(sampledWindowDurationMs)}`,
        `cgroup_memory_current_start_bytes=${unknown(first?.currentBytes ?? null)}`,
        `cgroup_memory_current_end_bytes=${unknown(last?.currentBytes ?? null)}`,
        `cgroup_memory_current_sampled_max_bytes=${unknown(cgroupMax("currentBytes"))}`,
        `cgroup_memory_peak_start_bytes=${unknown(first?.peakBytes ?? null)}`,
        `cgroup_memory_peak_end_bytes=${unknown(last?.peakBytes ?? null)}`,
        `cgroup_memory_peak_sampled_max_bytes=${unknown(cgroupMax("peakBytes"))}`,
        `memory_events_low_sampled_delta=${unknown(eventDelta("low"))}`,
        `memory_events_high_sampled_delta=${unknown(eventDelta("high"))}`,
        `memory_events_max_sampled_delta=${unknown(eventDelta("max"))}`,
        `memory_events_oom_sampled_delta=${unknown(eventDelta("oom"))}`,
        `memory_events_oom_kill_sampled_delta=${unknown(eventDelta("oom_kill"))}`,
        `memory_events_oom_group_kill_sampled_delta=${unknown(eventDelta("oom_group_kill"))}`,
        `gc_count=${gcCount}`, `gc_duration_ms=${Number.isFinite(gcDurationMs) ? gcDurationMs.toFixed(3) : "unknown"}`,
        `gc_status=${observer ? (gcOverflow ? "incomplete" : "complete") : "unavailable"}`,
        `gc_major_count=${gcCounts.major}`, `gc_minor_count=${gcCounts.minor}`,
        `gc_incremental_count=${gcCounts.incremental}`, `gc_weakcb_count=${gcCounts.weakcb}`,
        `gc_other_count=${gcCounts.other}`, `gc_overflow=${String(gcOverflow)}`,
      ];
      console.info(formatCpuDiagnosticMemoryEvent(tokens));
    } catch {
      // Diagnostic output is supplementary and must not affect the request.
    }
  };
  const finish = setTimeout(() => {
    let finalStatus: "finalized" | "failed" = "finalized";
    try {
      finalizeMemoryWindow();
    } catch (error) {
      finalStatus = "failed";
      throw error;
    } finally {
      emitSsrWebLifecycleEvent(plan, phase, "memory", finalStatus, {
        reason: finalStatus === "finalized" ? "none" : "end_error",
        sampleCount: samples.length,
      });
    }
  }, Math.max(1, phaseEndMonotonicMs - performance.now()));
  finish.unref();
}

type InspectorResponse<T> = { readonly result?: T };

async function startAuthorizedNodeCpuProfile(plan: DiagnosticPlan): Promise<void> {
  const startedAtMs = Date.now();
  const startLagMs = startedAtMs - plan.on_start_ms;
  if (consumedCpuRunIds.has(plan.run_id)) {
    emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "skipped", {
      reason: "already_claimed", lagMs: startLagMs,
    });
    return;
  }
  if (activeCpuRunId !== null) {
    emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "skipped", {
      reason: "already_claimed", lagMs: startLagMs,
    });
    return;
  }
  consumedCpuRunIds.add(plan.run_id);
  await startNodeCpuProfile(plan);
}

function scheduleWebCpuProfile(
  plan: DiagnosticPlan,
  phase: DiagnosticPhase | null,
): void {
  const runId = plan.run_id;
  if (plan.workload !== WORKLOAD_HTML_SSR_CPU_PROFILE_20S) return;
  if (phase === "on") {
    if (!consumedCpuRunIds.has(runId)) {
      consumedCpuRunIds.add(runId);
      webCpuProfileFailed.add(runId);
      emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "skipped", {
        reason: "late", lagMs: Date.now() - plan.on_start_ms,
      });
    }
    return;
  }
  if (phase !== null && phase !== "off") return;
  if (consumedCpuRunIds.has(runId)) return;
  consumedCpuRunIds.add(runId);
  const controller = scheduleWorkspaceCpuProfilePrearm<DiagnosticPlan, inspector.Session>(plan, {
    now: Date.now,
    schedule: (callback, delayMs) => {
      const timer = setTimeout(callback, delayMs);
      timer.unref();
      return timer;
    },
    cancelTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
    readPlan: safeReadPlan,
    samePlan: (expected, current) => current !== null
      && canonicalJson(expected) === canonicalJson(current),
    phaseAt: activePhase,
    prepare: async (_current, retainSession) => {
      const session = new inspector.Session();
      retainSession(session);
      session.connect();
      await post<InspectorResponse<unknown>>(session, "Profiler.enable");
      await post<InspectorResponse<unknown>>(session, "Profiler.setSamplingInterval", {
        interval: CPU_PROFILE_SAMPLING.intervalUs,
      });
      return session;
    },
    start: async (session) => {
      await post<InspectorResponse<unknown>>(session, "Profiler.start");
      return Date.now();
    },
    stop: async (session) => {
      await post<InspectorResponse<unknown>>(session, "Profiler.stop");
    },
    close: (session) => session.disconnect(),
    scheduled: webCpuProfileScheduled,
    started: webCpuProfileStarted,
    failed: webCpuProfileFailed,
    onStarted: (session, startedAtMs, profileController) => {
      const startLagMs = startedAtMs - plan.on_start_ms;
      const startedMonotonicMs = performance.now();
      activeCpuRunId = runId;
      const timer = setTimeout(() => {
        void stopNodeCpuProfile(
          session, runId, startLagMs, startedMonotonicMs, profileController,
        );
      }, Math.max(1, plan.on_end_ms - Date.now()));
      timer.unref();
    },
    onLifecycle: ({ status, reason, lagMs }) => {
      emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", status, { reason, lagMs });
      if (status === "failed" || status === "skipped") {
        consumedCpuRunIds.add(runId);
        if (activeCpuRunId === runId) activeCpuRunId = null;
      }
    },
    startToleranceMs: CPU_WINDOW_TOLERANCE_MS,
  });
  if (controller === null) {
    webCpuProfileFailed.add(runId);
    emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "skipped", {
      reason: "already_claimed",
    });
    return;
  }
}

function post<T>(session: inspector.Session, method: string, params?: object): Promise<T> {
  return new Promise((resolvePromise, rejectPromise) => {
    const compatible = session as unknown as {
      post: (name: string, args: object, callback: (error: Error | null, response: unknown) => void) => void;
    };
    compatible.post(method, params ?? {}, (error, response) => {
      if (error) rejectPromise(error);
      else resolvePromise(response as T);
    });
  });
}

async function startNodeCpuProfile(plan: DiagnosticPlan): Promise<void> {
  const session = new inspector.Session();
  let startLagMs = 0;
  let startedMonotonicMs = 0;
  try {
    session.connect();
    await post<InspectorResponse<unknown>>(session, "Profiler.enable");
    await post<InspectorResponse<unknown>>(session, "Profiler.setSamplingInterval", {
      interval: CPU_PROFILE_SAMPLING.intervalUs,
    });
    await post<InspectorResponse<unknown>>(session, "Profiler.start");
    const startedAtMs = Date.now();
    startLagMs = Math.max(0, startedAtMs - plan.on_start_ms);
    startedMonotonicMs = performance.now();
    activeCpuRunId = plan.run_id;
    emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "started", {
      lagMs: startLagMs,
    });
    const delay = Math.max(1, plan.on_end_ms - Date.now());
    const timer = setTimeout(() => {
      void stopNodeCpuProfile(session, plan.run_id, startLagMs, startedMonotonicMs);
    }, delay);
    timer.unref();
  } catch {
    if (activeCpuRunId === plan.run_id) activeCpuRunId = null;
    emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "failed", {
      reason: "start_error", lagMs: Date.now() - plan.on_start_ms,
    });
    try {
      session.disconnect();
    } catch {
      // Inspector cleanup is best-effort and never changes the SSR response.
    }
  }
}

async function stopNodeCpuProfile(
  session: inspector.Session,
  runId: string,
  startLagMs: number,
  startedMonotonicMs: number,
  profileController?: {
    complete: () => Promise<void>;
  },
): Promise<void> {
  if (activeCpuRunId !== runId) {
    emitSsrWebLifecycleEvent({ run_id: runId }, "on", "cpu_profile", "skipped", {
      reason: "already_claimed",
    });
    if (profileController) {
      await profileController.complete();
    } else {
      try { session.disconnect(); } catch { /* cleanup cannot affect the request */ }
    }
    return;
  }
  activeCpuRunId = null;
  let finalStatus: "finalized" | "failed" = "failed";
  let finalReason: "profile_bounds" | "profile_stop" | "none" = "profile_stop";
  try {
    const response = await post<{ profile?: {
      nodes?: Array<{ id: number; callFrame?: {
        url?: string; functionName?: string; lineNumber?: number; columnNumber?: number;
      } }>;
      samples?: number[];
      timeDeltas?: number[];
    } }>(session, "Profiler.stop");
    const elapsedAtStopMs = Math.max(0, Math.floor(performance.now() - startedMonotonicMs));
    const elapsedMs = elapsedAtStopMs;
    const profile = response.profile;
    if (!profile || !Array.isArray(profile.nodes) || !Array.isArray(profile.samples)
        || !Array.isArray(profile.timeDeltas) || profile.samples.length > MAX_SAMPLES
        || profile.samples.length !== profile.timeDeltas.length
        || profile.nodes.length > MAX_PROFILE_NODES
        || profile.timeDeltas.some((delta) => !Number.isSafeInteger(delta) || delta < 0)
        || profile.timeDeltas.reduce((total, delta) => total + delta, 0) > MAX_PROFILE_CPU_US) {
      console.info(`cpu_diagnostic_complete service=web run_id=${runId} status=incomplete reason=profile_bounds`);
      finalReason = "profile_bounds";
      return;
    }
    const profileNodes = profile.nodes;
    const profileSamples = profile.samples;
    const profileWindow = await mapAfterCpuWindowSnapshot(
      elapsedMs,
      waitForCpuUsageWindowSnapshot(runId, "on"),
      () => mapSampledFrames(
        new Map<number, CpuProfileFrame>(profileNodes.map((node) => [node.id, node.callFrame ?? {}])),
        profileSamples,
        process.cwd(),
      ),
    );
    const nodes = profileWindow.mapped;
    const totals = new Map<string, { samples: number; cpuUs: number }>();
    const sampleClassCounts = summarizeSampleClasses(nodes, profileSamples);
    const acceptedSamples = profileSamples.length;
    for (let index = 0; index < profileSamples.length; index += 1) {
      const frame = nodes.get(profileSamples[index]);
      const category = categoryForFrame(frame?.url ?? "", frame?.functionName ?? "");
      const row = totals.get(category) ?? { samples: 0, cpuUs: 0 };
      row.samples += 1;
      row.cpuUs += Math.max(0, profile.timeDeltas[index]);
      totals.set(category, row);
    }
    const otherTotals = totals.get("other") ?? { samples: 0, cpuUs: 0 };
    const summaryRows = [...totals.entries()]
      .filter(([name]) => name !== "other")
      .sort((left, right) => right[1].cpuUs - left[1].cpuUs || left[0].localeCompare(right[0]))
      .slice(0, 7)
      .map(([name, row]) => `${name}:${row.cpuUs}:${row.samples}`);
    summaryRows.push(`other:${otherTotals.cpuUs}:${otherTotals.samples}`);
    const summary = summaryRows.join(",");
    const functionDetails = summarizeFunctionSamples(nodes, profileSamples);
    const functionSummary = functionDetails.rows
      .map(({ function_id, samples }) => `${function_id}:${samples}`)
      .join(",");
    console.info(
      `cpu_diagnostic_complete service=web run_id=${runId} timer=v8_cpu `
      + `start_lag_ms=${startLagMs} elapsed_ms=${elapsedMs} `
      + `sample_interval_us=10000 sample_count=${acceptedSamples} `
      + `sample_classes=idle:${sampleClassCounts.idle},gc:${sampleClassCounts.garbage_collector},`
      + `program:${sampleClassCounts.program},unmapped_busy:${sampleClassCounts.unmapped_busy},`
      + `known_category:${sampleClassCounts.known_category},unclassified:${sampleClassCounts.unclassified} `
      + `categories=${summary.slice(0, 3000)} `
      + `top_samples=${functionSummary}`
    );
    finalStatus = "finalized";
    finalReason = "none";
  } catch {
    console.info(`cpu_diagnostic_complete service=web run_id=${runId} status=incomplete reason=profile_stop`);
    finalReason = "profile_stop";
  } finally {
    if (profileController) {
      await profileController.complete();
    } else {
      try {
        session.disconnect();
      } catch {
        // Inspector cleanup is best-effort and never changes the SSR response.
      }
    }
    emitSsrWebLifecycleEvent({ run_id: runId }, "on", "cpu_profile", finalStatus, {
      reason: finalReason,
      lagMs: startLagMs,
    });
  }
}
