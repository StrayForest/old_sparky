const SAMPLE_INTERVAL_MS = 5_000;
const MAXIMUM_SAMPLES = 5_760;
const MAXIMUM_DURATION_MS = 8 * 60 * 60 * 1_000;

export function startHeapMetrics(nodeProcess, perfHooks, runtime = {}) {
  const v8 = nodeProcess.getBuiltinModule?.("node:v8");
  if (!v8) {
    return () => {};
  }

  const setTimer = runtime.setInterval || setInterval;
  const clearTimer = runtime.clearInterval || clearInterval;
  const writeLine = runtime.writeLine || ((line) => console.info(line));
  const startedAt = perfHooks.performance.now();
  let sampleCount = 0;
  let gcCount = 0;
  let gcDurationMs = 0;
  let stopped = false;
  let timer;
  const observer = new perfHooks.PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (Number.isFinite(entry.duration) && entry.duration >= 0) {
        gcCount += 1;
        gcDurationMs += entry.duration;
      }
    }
  });
  observer.observe({ entryTypes: ["gc"] });

  const stop = () => {
    if (stopped) {
      return;
    }
    stopped = true;
    if (timer) {
      clearTimer(timer);
    }
    observer.disconnect();
    nodeProcess.removeListener("exit", stop);
  };

  timer = setTimer(() => {
    const elapsedMs = perfHooks.performance.now() - startedAt;
    if (sampleCount >= MAXIMUM_SAMPLES || elapsedMs > MAXIMUM_DURATION_MS) {
      stop();
      return;
    }
    const memory = nodeProcess.memoryUsage();
    const heap = v8.getHeapStatistics();
    const integerFields = [
      sampleCount + 1,
      memory.rss,
      heap.total_heap_size,
      heap.used_heap_size,
      heap.heap_size_limit,
      memory.external,
      memory.arrayBuffers,
      gcCount,
    ];
    if (
      !Number.isFinite(elapsedMs)
      || elapsedMs < 0
      || !Number.isFinite(gcDurationMs)
      || gcDurationMs < 0
      || integerFields.some((value) => !Number.isSafeInteger(value) || value < 0)
    ) {
      return;
    }

    sampleCount += 1;
    try {
      writeLine(
        `ssr_heap schema=1 sample=${sampleCount}`
          + ` elapsed_seconds=${(elapsedMs / 1_000).toFixed(3)}`
          + ` rss_bytes=${memory.rss}`
          + ` heap_total_bytes=${heap.total_heap_size}`
          + ` heap_used_bytes=${heap.used_heap_size}`
          + ` heap_limit_bytes=${heap.heap_size_limit}`
          + ` external_bytes=${memory.external}`
          + ` array_buffers_bytes=${memory.arrayBuffers}`
          + ` gc_count=${gcCount}`
          + ` gc_duration_ms=${gcDurationMs.toFixed(3)}`,
      );
    } catch {
      stop();
      return;
    }
    gcCount = 0;
    gcDurationMs = 0;
    if (sampleCount >= MAXIMUM_SAMPLES || elapsedMs >= MAXIMUM_DURATION_MS) {
      stop();
    }
  }, SAMPLE_INTERVAL_MS);
  timer.unref();
  nodeProcess.once("exit", stop);
  return stop;
}
