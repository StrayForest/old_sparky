import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";
import { constants as fsConstants, openSync, closeSync, fstatSync, lstatSync, readFileSync, readSync } from "node:fs";
import { availableParallelism } from "node:os";
import { resolve } from "node:path";
import inspector from "node:inspector";
import { headers } from "next/headers";
import { hasAuthorizedDiagnosticRunMarker } from "./performance-diagnostic-marker";

const PLAN_DIRECTORY = "/run/oldsparky-platform";
const PLAN_PATH = `${PLAN_DIRECTORY}/performance-diagnostic-plan.web.json`;
const MAX_PLAN_BYTES = 4096;
const MAX_TOTAL_WINDOW_MS = 60_000;
const OFF_WINDOW_MS = 20_000;
const IDLE_WINDOW_MS = 5_000;
const ON_WINDOW_MS = 20_000;
const MAX_SAMPLES = 1200;
const MAX_PROFILE_NODES = 10_000;
const MAX_PROFILE_CPU_US = 60_000_000;
const CPU_WINDOW_TOLERANCE_MS = 250;
const WORKLOAD = "authenticated_workspace_read_pair_v1";
const DIAGNOSTIC_TRACE_HEADER = "x-platform-ssr-trace";

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
  readonly workload: typeof WORKLOAD;
  readonly off_start_ms: number;
  readonly off_end_ms: number;
  readonly on_start_ms: number;
  readonly on_end_ms: number;
  readonly expires_at_ms: number;
  readonly targets: readonly PlanTarget[];
};

export type DiagnosticPhase = "off" | "on";
export type WorkspaceCpuDiagnostic = {
  readonly runId: string;
  readonly phase: DiagnosticPhase;
  readonly active: true;
};

type ActiveDiagnostic = {
  readonly plan: DiagnosticPlan;
  readonly phase: DiagnosticPhase;
};

const diagnosticStorage = new AsyncLocalStorage<ActiveDiagnostic>();
const consumedCpuRunIds = new Set<string>();
const usageWindowsStarted = new Set<string>();
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
  if (plan.schema !== 1 || plan.workload !== WORKLOAD
      || typeof plan.run_id !== "string" || !/^[0-9a-f]{32}$/u.test(plan.run_id)
      || typeof plan.source_sha !== "string" || !/^[0-9a-f]{40}$/u.test(plan.source_sha)
      || typeof plan.release_slug !== "string" || !/^[a-z0-9][a-z0-9-]{0,79}$/u.test(plan.release_slug)) {
    return null;
  }
  const timeNames = ["off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"] as const;
  const times = timeNames.map((name) => plan[name]);
  if (times.some((time) => typeof time !== "number" || !Number.isSafeInteger(time) || time <= 0)) return null;
  const [offStart, offEnd, onStart, onEnd, expires] = times as number[];
  if (!(offStart < offEnd && offEnd <= onStart && onStart < onEnd && onEnd === expires)
      || offEnd - offStart !== OFF_WINDOW_MS
      || onStart - offEnd !== IDLE_WINDOW_MS
      || onEnd - onStart !== ON_WINDOW_MS
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
    const releasePath = resolve(process.cwd(), "../../..", "RELEASE.json");
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
  return current && activePhase(current.plan, Date.now()) === current.phase
    ? current.plan.run_id
    : null;
}

export async function run_with_workspace_cpu_diagnostic<T>(
  operation: () => Promise<T>
): Promise<T> {
  const plan = currentPlan();
  if (!plan) return operation();
  const phase = activePhase(plan, Date.now());
  if (!phase) return operation();
  let incomingTrace: string | null = null;
  try {
    incomingTrace = (await headers()).get(DIAGNOSTIC_TRACE_HEADER);
  } catch {
    // A missing request context must never arm diagnostics.
    return operation();
  }
  if (!hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)) return operation();
  startCpuUsageWindow(plan, phase);
  if (phase === "on" && !consumedCpuRunIds.has(plan.run_id) && activeCpuRunId === null) {
    consumedCpuRunIds.add(plan.run_id);
    await startNodeCpuProfile(plan, Date.now());
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

function startCpuUsageWindow(plan: DiagnosticPlan, phase: DiagnosticPhase): void {
  const key = `${plan.run_id}:${phase}`;
  if (usageWindowsStarted.has(key)) return;
  usageWindowsStarted.add(key);
  const startTarget = phase === "off" ? plan.off_start_ms : plan.on_start_ms;
  const endTarget = phase === "off" ? plan.off_end_ms : plan.on_end_ms;
  const startMs = Date.now();
  const startLagMs = startMs - startTarget;
  const startCpu = process.cpuUsage();
  const startMonotonic = performance.now();
  const capacity = effectiveCpuCapacity();
  const timer = setTimeout(() => {
    const endMs = Date.now();
    const endLagMs = endMs - endTarget;
    const windowMs = Math.max(0, Math.round(performance.now() - startMonotonic));
    const usage = process.cpuUsage(startCpu);
    const cpuNs = Math.max(0, usage.user + usage.system) * 1000;
    const timingComplete = Boolean(
      startLagMs >= 0 && startLagMs <= CPU_WINDOW_TOLERANCE_MS
      && Math.abs(endLagMs) <= CPU_WINDOW_TOLERANCE_MS
      && windowMs >= 20_000 - CPU_WINDOW_TOLERANCE_MS
      && windowMs <= 20_000 + 2 * CPU_WINDOW_TOLERANCE_MS
    );
    console.info(
      `cpu_diagnostic_usage service=web run_id=${plan.run_id} phase=${phase}`
      + ` window_ms=${windowMs} cpu_ns=${cpuNs}`
      + ` cpu_capacity_cpus=${capacity === null ? "unknown" : capacity.toFixed(6)}`
      + ` start_lag_ms=${startLagMs} end_lag_ms=${endLagMs}`
      + ` timing_complete=${String(timingComplete)}`
    );
  }, Math.max(0, endTarget - startMs));
  timer.unref();
}

function categoryForFrame(url: string, functionName: string): string {
  const normalized = url.replaceAll("\\", "/");
  if (normalized.includes("/apps/platform_web/.next/server/app/(site)/tournaments/")) {
    if (["getTournamentWorkspace", "loadTournamentWorkspace", "TournamentPage"].includes(functionName)) {
      return `repo.${functionName === "getTournamentWorkspace" ? "workspace_api_fetch" : "workspace_page"}`;
    }
  }
  if (normalized.includes("/node_modules/next/")) return "web_framework";
  if (normalized.includes("/node_modules/react/")) return "serialization_validation";
  if (normalized.includes("/node_modules/zod/")) return "serialization_validation";
  if (normalized.startsWith("node:internal/")) return "async_event_loop";
  if (normalized.includes("/node_modules/undici/")) return "http_client";
  if (normalized.startsWith("node:crypto") || normalized.includes("/node:crypto")) return "crypto";
  return "other";
}

type InspectorResponse<T> = { readonly result?: T };

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

async function startNodeCpuProfile(plan: DiagnosticPlan, startedAtMs: number): Promise<void> {
  const session = new inspector.Session();
  const startLagMs = Math.max(0, startedAtMs - plan.on_start_ms);
  const startedMonotonicMs = performance.now();
  try {
    session.connect();
    await post<InspectorResponse<unknown>>(session, "Profiler.enable");
    await post<InspectorResponse<unknown>>(session, "Profiler.setSamplingInterval", { interval: 100_000 });
    await post<InspectorResponse<unknown>>(session, "Profiler.start");
    activeCpuRunId = plan.run_id;
    const delay = Math.max(1, plan.on_end_ms - Date.now());
    const timer = setTimeout(() => {
      void stopNodeCpuProfile(session, plan.run_id, startLagMs, startedMonotonicMs);
    }, delay);
    timer.unref();
  } catch {
    session.disconnect();
  }
}

async function stopNodeCpuProfile(
  session: inspector.Session,
  runId: string,
  startLagMs: number,
  startedMonotonicMs: number
): Promise<void> {
  if (activeCpuRunId !== runId) return;
  activeCpuRunId = null;
  try {
    const response = await post<{ profile?: {
      nodes?: Array<{ id: number; callFrame?: { url?: string; functionName?: string } }>;
      samples?: number[];
      timeDeltas?: number[];
    } }>(session, "Profiler.stop");
    const profile = response.profile;
    if (!profile || !Array.isArray(profile.nodes) || !Array.isArray(profile.samples)
        || !Array.isArray(profile.timeDeltas) || profile.samples.length > MAX_SAMPLES
        || profile.samples.length !== profile.timeDeltas.length
        || profile.nodes.length > MAX_PROFILE_NODES
        || profile.timeDeltas.some((delta) => !Number.isSafeInteger(delta) || delta < 0)
        || profile.timeDeltas.reduce((total, delta) => total + delta, 0) > MAX_PROFILE_CPU_US) {
      console.info(`cpu_diagnostic_complete service=web run_id=${runId} status=incomplete reason=profile_bounds`);
      return;
    }
    const nodes = new Map(profile.nodes.map((node) => [node.id, node.callFrame ?? {}]));
    const totals = new Map<string, { samples: number; cpuUs: number }>();
    let acceptedSamples = 0;
    for (let index = 0; index < profile.samples.length; index += 1) {
      const frame = nodes.get(profile.samples[index]);
      if (!frame) continue;
      const category = categoryForFrame(frame.url ?? "", frame.functionName ?? "");
      const row = totals.get(category) ?? { samples: 0, cpuUs: 0 };
      row.samples += 1;
      row.cpuUs += Math.max(0, profile.timeDeltas[index]);
      totals.set(category, row);
      acceptedSamples += 1;
    }
    const summary = [...totals.entries()]
      .sort((left, right) => right[1].cpuUs - left[1].cpuUs || left[0].localeCompare(right[0]))
      .slice(0, 8)
      .map(([name, row]) => `${name}:${row.cpuUs}:${row.samples}`)
      .join(",");
    const elapsedMs = Math.max(0, Math.floor(performance.now() - startedMonotonicMs));
    console.info(
      `cpu_diagnostic_complete service=web run_id=${runId} timer=v8_cpu `
      + `start_lag_ms=${startLagMs} elapsed_ms=${elapsedMs} `
      + `sample_interval_us=100000 sample_count=${acceptedSamples} categories=${summary.slice(0, 3000)}`
    );
  } catch {
    console.info(`cpu_diagnostic_complete service=web run_id=${runId} status=incomplete reason=profile_stop`);
  } finally {
    session.disconnect();
  }
}

export const __diagnosticPlanTestHooks = {
  canonicalJson,
  validatePayload,
  categoryForFrame,
};
