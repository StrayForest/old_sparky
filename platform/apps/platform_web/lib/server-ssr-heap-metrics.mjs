const SAMPLE_INTERVAL_MS = 5_000;
const MAXIMUM_SAMPLES = 5_760;
const MAXIMUM_DURATION_MS = 8 * 60 * 60 * 1_000;

export function isStableBoundedPseudoFileRead({
  beforeSize,
  afterSize,
  beforeDevice,
  afterDevice,
  beforeInode,
  afterInode,
  beforeRegularFile,
  afterRegularFile,
  byteLength,
  limit,
}) {
  if (
    !Number.isSafeInteger(beforeSize) || beforeSize < 0
    || !Number.isSafeInteger(afterSize) || afterSize < 0
    || !Number.isSafeInteger(beforeDevice) || !Number.isSafeInteger(afterDevice)
    || !Number.isSafeInteger(beforeInode) || !Number.isSafeInteger(afterInode)
    || beforeRegularFile !== true || afterRegularFile !== true
    || !Number.isSafeInteger(byteLength) || byteLength <= 0
    || !Number.isSafeInteger(limit) || limit <= 0
    || byteLength > limit
    || beforeDevice !== afterDevice
    || beforeInode !== afterInode
    || beforeSize !== afterSize
  ) {
    return false;
  }
  return beforeSize === 0 || beforeSize === byteLength;
}

export function appendBoundedSample(samples, value, limit) {
  if (!Array.isArray(samples) || !Number.isSafeInteger(limit) || limit <= 0) {
    throw new TypeError("bounded sample input is invalid");
  }
  if (samples.length >= limit) return false;
  samples.push(value);
  return true;
}

export function isTimelineEntryWithinWindow(startTime, windowStart, windowEnd) {
  return Number.isFinite(startTime)
    && Number.isFinite(windowStart)
    && Number.isFinite(windowEnd)
    && windowStart <= startTime
    && startTime <= windowEnd;
}

export function timelineEntryDurationWithinWindow(startTime, duration, windowStart, windowEnd) {
  if (
    !isTimelineEntryWithinWindow(startTime, windowStart, windowEnd)
    || !Number.isFinite(duration) || duration < 0
  ) return null;
  return Math.max(0, Math.min(startTime + duration, windowEnd) - startTime);
}

export function classifyCgroupReadFailure(code) {
  return code === "ENOENT" || code === "ENOTDIR" ? "unavailable" : "invalid";
}

export function formatCpuDiagnosticMemoryEvent(tokens) {
  if (!Array.isArray(tokens) || tokens.length < 8) {
    throw new TypeError("CPU memory event is invalid");
  }
  const seen = new Set();
  for (const token of tokens) {
    if (typeof token !== "string" || !/^[a-z][a-z0-9_]*=[A-Za-z0-9_.-]+$/u.test(token)) {
      throw new TypeError("CPU memory event is invalid");
    }
    const key = token.slice(0, token.indexOf("="));
    if (seen.has(key)) throw new TypeError("CPU memory event is invalid");
    seen.add(key);
  }
  return `cpu_diagnostic_memory ${tokens.join(" ")}`;
}

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
