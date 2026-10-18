import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  appendBoundedSample,
  classifyCgroupReadFailure,
  formatCpuDiagnosticMemoryEvent,
  isStableBoundedPseudoFileRead,
  isTimelineEntryWithinWindow,
  timelineEntryDurationWithinWindow,
  startHeapMetrics,
} from "../lib/server-ssr-heap-metrics.mjs";
import { openSync, closeSync, fstatSync, readSync, readFileSync, constants as fsConstants } from "node:fs";

{
  const tokens = ["service=web", `run_id=${"a".repeat(32)}`, "phase=off", "sample_count=5", "status=complete"];
  for (const prefix of ["rss", "heap_total", "heap_used", "heap_limit", "external", "array_buffers"]) {
    for (const suffix of ["min", "max", "mean"]) tokens.push(`${prefix}_${suffix}_bytes=100`);
  }
  tokens.push(
    "cgroup_sample_count=5", "cgroup_memory_status=complete", "cgroup_memory_reason=none",
    "sampled_window_start_elapsed_ms=0", "sampled_window_end_elapsed_ms=16000",
    "sampled_window_duration_ms=16000", "cgroup_memory_current_start_bytes=1000",
    "cgroup_memory_current_end_bytes=1100", "cgroup_memory_current_sampled_max_bytes=1100",
    "cgroup_memory_peak_start_bytes=1200", "cgroup_memory_peak_end_bytes=1200",
    "cgroup_memory_peak_sampled_max_bytes=1200", "memory_events_low_sampled_delta=0",
    "memory_events_high_sampled_delta=0", "memory_events_max_sampled_delta=0",
    "memory_events_oom_sampled_delta=0", "memory_events_oom_kill_sampled_delta=0",
    "memory_events_oom_group_kill_sampled_delta=0", "gc_count=0", "gc_duration_ms=0.000",
    "gc_status=complete", "gc_major_count=0", "gc_minor_count=0", "gc_incremental_count=0",
    "gc_weakcb_count=0", "gc_other_count=0", "gc_overflow=false",
  );
  const fixturePath = new URL("../../../tests/fixtures/cpu-diagnostic-memory-complete-v1.protocol", import.meta.url);
  const fixture = readFileSync(fixturePath, "ascii").trim();
  assert.equal(formatCpuDiagnosticMemoryEvent(tokens), fixture);
}

assert.equal(classifyCgroupReadFailure("ENOENT"), "unavailable");
assert.equal(classifyCgroupReadFailure("ENOTDIR"), "unavailable");
assert.equal(classifyCgroupReadFailure("EACCES"), "invalid");
assert.equal(classifyCgroupReadFailure("EPERM"), "invalid");
assert.equal(classifyCgroupReadFailure(null), "invalid");

{
  const pseudoFile = {
    beforeSize: 0,
    afterSize: 0,
    beforeDevice: 12,
    afterDevice: 12,
    beforeInode: 34,
    afterInode: 34,
    beforeRegularFile: true,
    afterRegularFile: true,
    byteLength: 23,
    limit: 128,
  };
  assert.equal(isStableBoundedPseudoFileRead(pseudoFile), true);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, byteLength: 0 }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, byteLength: 129 }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, afterSize: 1 }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, afterInode: 35 }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, afterDevice: 13 }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, beforeRegularFile: false }), false);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, beforeSize: 23, afterSize: 23 }), true);
  assert.equal(isStableBoundedPseudoFileRead({ ...pseudoFile, beforeSize: 24, afterSize: 24 }), false);
}

{
  const samples = [];
  for (let index = 0; index < 5; index += 1) {
    assert.equal(appendBoundedSample(samples, index, 5), true);
  }
  assert.equal(appendBoundedSample(samples, 5, 5), false);
  assert.deepEqual(samples, [0, 1, 2, 3, 4]);
  assert.equal(isTimelineEntryWithinWindow(10, 10, 20), true);
  assert.equal(isTimelineEntryWithinWindow(20, 10, 20), true);
  assert.equal(isTimelineEntryWithinWindow(9.999, 10, 20), false);
  assert.equal(isTimelineEntryWithinWindow(20.001, 10, 20), false);
  assert.equal(timelineEntryDurationWithinWindow(10, 15, 10, 20), 10);
  assert.equal(timelineEntryDurationWithinWindow(9, 15, 10, 20), null);
  assert.equal(timelineEntryDurationWithinWindow(20, 15, 10, 20), 0);
}

{
  const path = "/proc/self/cgroup";
  const limit = 8_192;
  const fd = openSync(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
  try {
    const before = fstatSync(fd);
    const buffer = Buffer.alloc(limit + 1);
    const length = readSync(fd, buffer, 0, limit + 1, 0);
    const after = fstatSync(fd);
    assert.ok(length > 0);
    assert.ok(length <= limit);
    assert.equal(before.size, 0);
    assert.equal(after.size, 0);
    assert.equal(isStableBoundedPseudoFileRead({
      beforeSize: before.size,
      afterSize: after.size,
      beforeDevice: before.dev,
      afterDevice: after.dev,
      beforeInode: before.ino,
      afterInode: after.ino,
      beforeRegularFile: before.isFile(),
      afterRegularFile: after.isFile(),
      byteLength: length,
      limit,
    }), true);
    const cgroupRecord = buffer.subarray(0, length).toString("ascii");
    assert.match(cgroupRecord, /^0::\//m);
  } finally {
    closeSync(fd);
  }
}

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
