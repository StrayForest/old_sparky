import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { startHeapMetrics } from "../lib/server-ssr-heap-metrics.mjs";

function fixture({ writeLine } = {}) {
  let now = 0;
  let callback;
  let intervalMs;
  let cleared = false;
  let unreferenced = false;
  let disconnected = false;
  let observerCallback;
  const lines = [];
  const nodeProcess = new EventEmitter();
  nodeProcess.getBuiltinModule = (name) => {
    assert.equal(name, "node:v8");
    return {
      getHeapStatistics: () => ({
        total_heap_size: 80_000,
        used_heap_size: 50_000,
        heap_size_limit: 512_000_000,
      }),
    };
  };
  nodeProcess.memoryUsage = () => ({
    rss: 100_000,
    external: 25_000,
    arrayBuffers: 12_000,
  });
  const perfHooks = {
    performance: { now: () => now },
    PerformanceObserver: class {
      constructor(callbackFunction) {
        observerCallback = callbackFunction;
      }
      observe(options) {
        assert.deepEqual(options, { entryTypes: ["gc"] });
      }
      disconnect() {
        disconnected = true;
      }
    },
  };
  const stop = startHeapMetrics(nodeProcess, perfHooks, {
    setInterval: (fn, ms) => {
      callback = fn;
      intervalMs = ms;
      return { unref() { unreferenced = true; } };
    },
    clearInterval: () => {
      cleared = true;
    },
    writeLine: writeLine || ((line) => lines.push(line)),
  });
  return {
    lines,
    nodeProcess,
    setNow: (value) => { now = value; },
    tick: () => callback(),
    emitGc: (entries) => observerCallback({ getEntries: () => entries }),
    intervalMs: () => intervalMs,
    isCleared: () => cleared,
    isUnreferenced: () => unreferenced,
    isDisconnected: () => disconnected,
    stop,
  };
}

{
  const test = fixture();
  assert.equal(test.intervalMs(), 5_000);
  assert.equal(test.isUnreferenced(), true);
  test.emitGc([{ duration: 2.5 }, { duration: 1.25 }, { duration: Infinity }]);
  test.setNow(5_000);
  test.tick();
  assert.equal(test.lines.length, 1);
  assert.match(
    test.lines[0],
    /^ssr_heap schema=1 sample=1 elapsed_seconds=5\.000 rss_bytes=100000 heap_total_bytes=80000 heap_used_bytes=50000 heap_limit_bytes=512000000 external_bytes=25000 array_buffers_bytes=12000 gc_count=2 gc_duration_ms=3\.750$/,
  );
  assert.doesNotMatch(test.lines[0], /pid=|url=|request|cookie|secret|exception/i);
  test.nodeProcess.emit("exit");
  assert.equal(test.isCleared(), true);
  assert.equal(test.isDisconnected(), true);
}

{
  const test = fixture();
  for (let sample = 1; sample <= 5_760; sample += 1) {
    test.setNow(sample * 5_000);
    test.tick();
  }
  assert.equal(test.lines.length, 5_760);
  assert.match(test.lines.at(-1), /sample=5760 /);
  assert.equal(test.isCleared(), true);
  assert.equal(test.isDisconnected(), true);
  test.setNow(8 * 60 * 60 * 1_000 + 5_000);
  test.tick();
  assert.equal(test.lines.length, 5_760);
}

{
  const test = fixture();
  test.setNow(8 * 60 * 60 * 1_000 + 1);
  test.tick();
  assert.equal(test.lines.length, 0);
  assert.equal(test.isCleared(), true);
  assert.equal(test.isDisconnected(), true);
}

{
  const test = fixture({ writeLine: () => { throw new Error("private logger failure"); } });
  test.setNow(5_000);
  test.tick();
  assert.equal(test.lines.length, 0);
  assert.equal(test.isCleared(), true);
  assert.equal(test.isDisconnected(), true);
}

console.log("SSR heap telemetry contract: PASS");
