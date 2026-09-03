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
  const timer = setInterval(() => {
    console.info(
      `ssr_event_loop p50_ms=${milliseconds(histogram.percentile(50))}`
        + ` p95_ms=${milliseconds(histogram.percentile(95))}`
        + ` max_ms=${milliseconds(histogram.max)}`
        + ` mean_ms=${milliseconds(histogram.mean)}`
    );
    histogram.reset();
  }, intervalSeconds() * 1_000);
  timer.unref();
}
