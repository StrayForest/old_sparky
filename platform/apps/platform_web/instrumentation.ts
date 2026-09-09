const eventLoopLoggingEnabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
let eventLoopMonitorStarted = false;
type NodePerfHooks = typeof import("node:perf_hooks");
type NodeRuntimeProcess = typeof process & {
  getBuiltinModule?: (id: string) => object | undefined;
};

function intervalSeconds(): number {
  const value = Number(process.env.PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS);
  if (!Number.isFinite(value)) {
    return 5;
  }
  return Math.min(60, Math.max(1, value));
}

function milliseconds(value: number): string {
  return Number.isFinite(value) ? (value / 1_000_000).toFixed(3) : "0.000";
}

export async function register(): Promise<void> {
  if (
    !eventLoopLoggingEnabled
    || process.env.NEXT_RUNTIME !== "nodejs"
    || eventLoopMonitorStarted
  ) {
    return;
  }
  eventLoopMonitorStarted = true;
  // Resolve the builtin only after Next has selected the Node runtime. A
  // runtime lookup keeps the Edge bundle free of a Node-only module.
  const nodeProcess = (globalThis as typeof globalThis & {
    process?: NodeRuntimeProcess;
  }).process;
  const perfHooks = nodeProcess?.getBuiltinModule?.("node:perf_hooks") as NodePerfHooks | undefined;
  if (!perfHooks) {
    return;
  }
  const { monitorEventLoopDelay } = perfHooks;
  const histogram = monitorEventLoopDelay({ resolution: 20 });
  histogram.enable();
  let previousEventLoopUtilization = perfHooks.performance.eventLoopUtilization();
  let previousCpuUsage = nodeProcess.cpuUsage();
  let gcCount = 0;
  let gcDurationMs = 0;
  const gcObserver = new perfHooks.PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      gcCount += 1;
      gcDurationMs += entry.duration;
    }
  });
  gcObserver.observe({ entryTypes: ["gc"] });
  let previousSampleAt = perfHooks.performance.now();
  setInterval(() => {
    const now = perfHooks.performance.now();
    const elapsedMs = Math.max(1, now - previousSampleAt);
    previousSampleAt = now;
    const currentEventLoopUtilization = perfHooks.performance.eventLoopUtilization();
    const eventLoopUtilization = perfHooks.performance.eventLoopUtilization(
      previousEventLoopUtilization,
      currentEventLoopUtilization
    );
    previousEventLoopUtilization = currentEventLoopUtilization;
    const cpuUsage = nodeProcess.cpuUsage(previousCpuUsage);
    previousCpuUsage = nodeProcess.cpuUsage();
    const cpuUserMs = cpuUsage.user / 1_000;
    const cpuSystemMs = cpuUsage.system / 1_000;
    const cpuPercent = ((cpuUserMs + cpuSystemMs) / elapsedMs) * 100;
    console.info(
      `ssr_event_loop p50_ms=${milliseconds(histogram.percentile(50))}`
        + ` p95_ms=${milliseconds(histogram.percentile(95))}`
        + ` p99_ms=${milliseconds(histogram.percentile(99))}`
        + ` max_ms=${milliseconds(histogram.max)}`
        + ` mean_ms=${milliseconds(histogram.mean)}`
        + ` elu=${eventLoopUtilization.utilization.toFixed(6)}`
        + ` cpu_pct=${cpuPercent.toFixed(3)}`
        + ` cpu_user_ms=${cpuUserMs.toFixed(3)}`
        + ` cpu_system_ms=${cpuSystemMs.toFixed(3)}`
        + ` gc_count=${gcCount}`
        + ` gc_duration_ms=${gcDurationMs.toFixed(3)}`
    );
    histogram.reset();
    gcCount = 0;
    gcDurationMs = 0;
  }, intervalSeconds() * 1_000).unref();
}
