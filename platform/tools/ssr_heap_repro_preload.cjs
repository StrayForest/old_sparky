"use strict";

// This preload is loaded only by the isolated, networkless diagnostic. It
// emits a fixed-schema, 1 Hz aggregate sample and retains no request data.
const { PerformanceObserver, monitorEventLoopDelay } = require("node:perf_hooks");
const v8 = require("node:v8");

const prefix = "SSR_HEAP_SAMPLE ";
const loop = monitorEventLoopDelay({ resolution: 20 });
loop.enable();
let gcCount = 0;
let gcDurationMs = 0;
let gcMaxDurationMs = 0;
const observer = new PerformanceObserver((list) => {
  for (const entry of list.getEntries()) {
    gcCount += 1;
    gcDurationMs += Number.isFinite(entry.duration) ? entry.duration : 0;
    gcMaxDurationMs = Math.max(gcMaxDurationMs, Number.isFinite(entry.duration) ? entry.duration : 0);
  }
});
observer.observe({ entryTypes: ["gc"] });

const interval = setInterval(() => {
  const memory = process.memoryUsage();
  const heap = v8.getHeapStatistics();
  const sample = {
    schema: 1,
    t_ms: Math.round(performance.now()),
    rss: memory.rss,
    heap_used: memory.heapUsed,
    heap_total: memory.heapTotal,
    external: memory.external,
    array_buffers: memory.arrayBuffers,
    heap_limit: heap.heap_size_limit,
    gc_count: gcCount,
    gc_duration_ms: gcDurationMs,
    gc_max_duration_ms: gcMaxDurationMs,
    event_loop_p95_ms: Number.isFinite(loop.percentile(95)) ? loop.percentile(95) / 1e6 : 0,
    event_loop_max_ms: Number.isFinite(loop.max) ? loop.max / 1e6 : 0,
  };
  gcCount = 0;
  gcDurationMs = 0;
  gcMaxDurationMs = 0;
  loop.reset();
  process.stderr.write(prefix + JSON.stringify(sample) + "\n");
}, 1000);
interval.unref();

process.on("exit", () => {
  clearInterval(interval);
  observer.disconnect();
  loop.disable();
});
