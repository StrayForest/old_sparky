"use strict";

const childProcess = process.getBuiltinModule("node:child_process");
const fs = process.getBuiltinModule("node:fs");
const path = process.getBuiltinModule("node:path");
const os = process.getBuiltinModule("node:os");
const assert = process.getBuiltinModule("node:assert/strict");

const guardPath = path.resolve(__dirname, "..", "server-shutdown-guard.cjs");
const fixtureWatchdogMs = 3_000;
const behaviorTimeoutMs = 5_000;
const cleanupTimeoutMs = 1_000;

function capacityCloseFixtureSource() {
  return `
const http = process.getBuiltinModule("node:http");
const crypto = process.getBuiltinModule("node:crypto");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
const runId = "0123456789abcdef0123456789abcdef";
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
function captureEligible(requestId) {
  return (crypto.createHash("sha256").update(runId + ":" + requestId, "ascii").digest()[0] & 7) === 0;
}
const noneligibleIds = [];
let eligibleId = null;
for (let candidate = 0; noneligibleIds.length < 128 || eligibleId === null; candidate += 1) {
  const requestId = candidate.toString(16).padStart(32, "0");
  if (captureEligible(requestId)) {
    if (eligibleId === null) eligibleId = requestId;
  } else if (noneligibleIds.length < 128) {
    noneligibleIds.push(requestId);
  }
}
const closedTags = new Set();
const closeWaiters = new Map();
function waitForClose(tag) {
  if (closedTags.has(tag)) return Promise.resolve();
  return new Promise((resolve) => closeWaiters.set(tag, resolve));
}
async function sendSelection(port, tag, requestId) {
  const closed = waitForClose(tag);
  const response = await fetch("http://127.0.0.1:" + port + "/select", {
    headers: {
      "x-request-id": requestId,
      "x-platform-ssr-trace": runId,
      "x-fixture-tag": tag,
      accept: "text/html",
    },
  });
  await response.text();
  await closed;
  return response.headers.get("x-fixture-selector-result");
}
const server = http.createServer((request, response) => {
  const requestId = request.headers["x-request-id"];
  const tag = request.headers["x-fixture-tag"];
  const expectedCaptureEligible = captureEligible(requestId);
  const accepted = selectPlanRequest({
    requestId,
    runId,
    expiresAtMs: Date.now() + 1_000,
    captureEligible: tag === "eligibility-mismatch"
      ? !expectedCaptureEligible
      : expectedCaptureEligible,
  });
  response.setHeader("x-fixture-selector-result", accepted ? "accepted" : "rejected");
  response.once("close", () => {
    const tag = request.headers["x-fixture-tag"];
    closedTags.add(tag);
    closeWaiters.get(tag)?.();
    closeWaiters.delete(tag);
  });
  response.writeHead(200, { "content-type": "text/html" });
  response.end("ok");
});
server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  try {
    const invalidResult = await sendSelection(port, "eligibility-mismatch", eligibleId);
    if (invalidResult !== "rejected") {
      throw new Error("capture eligibility mismatch was accepted");
    }
    for (let index = 0; index < noneligibleIds.length; index += 1) {
      const result = await sendSelection(port, "closed-noneligible-" + index, noneligibleIds[index]);
      if (result !== "accepted") throw new Error("closed noneligible selection was refused at index " + index);
    }
    const eligibleResult = await sendSelection(port, "eligible-after-closed-noneligible", eligibleId);
    if (eligibleResult !== "accepted") throw new Error("eligible selection was starved after closed noneligible responses");
    const eligibleAgain = await sendSelection(port, "eligible-after-close", eligibleId);
    if (eligibleAgain !== "accepted") throw new Error("eligible selection was not removed on response close");
    await new Promise((resolve) => setTimeout(resolve, 1_100));
    console.log("closed_noneligible=128 eligible_after_close=accepted eligible_again=accepted");
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
`;
}

function duplicateSelectionCloseFixtureSource() {
  return `
const http = process.getBuiltinModule("node:http");
const crypto = process.getBuiltinModule("node:crypto");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
const firstRunId = "0123456789abcdef0123456789abcdef";
const secondRunId = "fedcba9876543210fedcba9876543210";
let requestId = null;
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
for (let candidate = 0; requestId === null; candidate += 1) {
  const candidateId = candidate.toString(16).padStart(32, "0");
  const firstEligible = (crypto.createHash("sha256").update(firstRunId + ":" + candidateId, "ascii").digest()[0] & 7) === 0;
  const secondEligible = (crypto.createHash("sha256").update(secondRunId + ":" + candidateId, "ascii").digest()[0] & 7) === 0;
  if (firstEligible && secondEligible) {
    requestId = candidateId;
  }
}
let firstResponse = null;
let secondResponse = null;
let firstClosed = false;
function finishSecond() {
  if (!firstClosed || !secondResponse || secondResponse.headersSent) return;
  secondResponse.writeHead(200, { "content-type": "text/html" });
  secondResponse.end("second");
}
const server = http.createServer((request, response) => {
  const tag = request.headers["x-fixture-tag"];
  const runId = tag === "first" ? firstRunId : secondRunId;
  const accepted = selectPlanRequest({
    requestId,
    runId,
    expiresAtMs: Date.now() + 10_000,
    captureEligible: true,
  });
  response.setHeader("x-fixture-selector-result", accepted ? "accepted" : "rejected");
  if (tag === "first") {
    firstResponse = response;
    response.writeHead(200, { "content-type": "text/html" });
    response.write("first-open");
    response.once("close", () => {
      firstClosed = true;
      finishSecond();
    });
    return;
  }
  if (tag === "second") {
    secondResponse = response;
    firstResponse?.end("first-close");
    finishSecond();
    return;
  }
  response.writeHead(404);
  response.end();
});
server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  try {
    const commonHeaders = { "x-request-id": requestId, accept: "text/html" };
    const first = await fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
      headers: { ...commonHeaders, "x-platform-ssr-trace": firstRunId, "x-fixture-tag": "first" },
    });
    if (first.status !== 200 || first.headers.get("x-fixture-selector-result") !== "accepted") {
      throw new Error("first duplicate-id selection failed");
    }
    const firstBody = first.text();
    const second = await fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
      headers: { ...commonHeaders, "x-platform-ssr-trace": secondRunId, "x-fixture-tag": "second" },
    });
    if (second.status !== 200 || second.headers.get("x-fixture-selector-result") !== "accepted") {
      throw new Error("replacement duplicate-id selection failed");
    }
    await Promise.all([firstBody, second.text()]);
    console.log("duplicate_id_replacement=accepted");
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
`;
}

function bounded(value) {
  return String(value || "").slice(0, 4_000);
}

async function verifySsrWebLifecycleContract() {
  const scheduler = await import(path.resolve(
    __dirname, "..", "lib", "workspace-cpu-window-scheduler.mjs",
  ));
  const plan = { run_id: "0123456789abcdef0123456789abcdef" };
  const lines = [];
  const writeLine = (line) => lines.push(line);
  const event = (phase, name, status, options = {}) => scheduler.emitSsrWebLifecycleEvent(
    plan, phase, name, status, { ...options, writeLine, nowMs: Date.now() },
  );

  assert.equal(event("none", "mode", "trace_only"), true);
  assert.equal(event("none", "mode", "trace_only"), false);
  assert.equal(event("on", "mode", "active"), true);
  assert.equal(event("on", "cpu_window", "scheduled", { lagMs: -100 }), true);
  assert.equal(event("on", "cpu_window", "started", { lagMs: 250 }), true);
  assert.equal(event("on", "memory", "started"), true);
  assert.equal(event("on", "memory", "finalized", { sampleCount: 3 }), true);
  assert.equal(event("off", "cpu_window", "started"), true);
  assert.equal(event("off", "cpu_window", "finalized"), true);
  assert.equal(event("off", "cpu_profile", "started"), true);
  assert.equal(event("off", "cpu_profile", "failed", { reason: "profile_stop" }), true);
  assert.equal(event("off", "memory", "started"), true);
  assert.equal(event("off", "memory", "failed", { reason: "end_error" }), true);
  assert.equal(event("off", "memory", "failed", { reason: "end_error" }), false);
  assert.equal(event("off", "memory", "finalized", { sampleCount: 5 }), true);
  assert.equal(event("on", "memory", "finalized", { sampleCount: 3 }), false);
  assert.equal(scheduler.emitSsrWebLifecycleEvent(null, "on", "mode", "active", { writeLine }), false);
  assert.equal(scheduler.emitSsrWebLifecycleEvent({ run_id: "bad" }, "on", "mode", "active", { writeLine }), false);
  assert.equal(scheduler.emitSsrWebLifecycleEvent(plan, "invalid", "mode", "active", { writeLine }), false);
  assert.equal(event("on", "cpu_window", "failed", { reason: "/private/path" }), false);
  assert.equal(event("on", "memory", "finalized", { sampleCount: 6 }), false);
  assert.equal(scheduler.emitSsrWebLifecycleEvent(
    { run_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", expires_at_ms: Date.now() - 1 },
    "on", "memory", "finalized", { sampleCount: 2, writeLine },
  ), true);
  assert(lines.every((line) => /^ssr_web_lifecycle service=web run_id=[0-9a-f]{32} /u.test(line)));
  assert(lines.some((line) => line.includes("phase=none event=mode status=trace_only")));
  assert(lines.some((line) => line.includes("lag_ms=250")));
  assert(lines.some((line) => line.includes("sample_count=3")));
  assert(lines.some((line) => line.includes("event=memory status=started")));
  assert(lines.some((line) => line.includes("event=memory status=failed reason=end_error")));
  assert(lines.every((line) => !/(?:private|cookie|token|request|email|source-map|\.js)/iu.test(line)));
  assert(lines.every((line) => !line.startsWith("cpu_diagnostic_")));

  const boundedNow = Date.now() + 1;
  let accepted = 0;
  for (let index = 0; index < 300; index += 1) {
    const id = index.toString(16).padStart(32, "0");
    if (scheduler.emitSsrWebLifecycleEvent(
      { run_id: id }, "off", "mode", "active", { nowMs: boundedNow, writeLine },
    )) accepted += 1;
  }
  assert(accepted <= 256);
  assert.equal(scheduler.emitSsrWebLifecycleEvent(
    { run_id: "ffffffffffffffffffffffffffffffff" }, "off", "mode", "active",
    { nowMs: boundedNow + 600_001, writeLine },
  ), true);

  const schedulerPlan = {
    run_id: "fedcba9876543210fedcba9876543210",
    off_start_ms: 1_000, off_end_ms: 21_000,
    on_start_ms: 26_000, on_end_ms: 46_000, expires_at_ms: 46_000,
  };
  function scheduleCase(offsetMs, { stale = false, throwStart = false, throwSchedule = false } = {}) {
    let nowMs = 900;
    let current = schedulerPlan;
    if (stale) current = { ...schedulerPlan, expires_at_ms: schedulerPlan.expires_at_ms + 1 };
    const scheduled = new Set();
    const started = new Set();
    const failed = new Set();
    const events = [];
    let callback;
    const ok = scheduler.scheduleWorkspaceCpuWindowStart(schedulerPlan, "off", {
      now: () => nowMs,
      schedule: (next) => {
        if (throwSchedule) throw new Error("private setup detail");
        callback = next;
      },
      readPlan: () => current,
      samePlan: (expected, actual) => actual !== null
        && JSON.stringify(expected) === JSON.stringify(actual),
      phaseAt: (_current, atMs) => atMs >= 1_000 && atMs < 21_000 ? "off" : null,
      startTarget: (_current, phase) => phase === "off" ? 1_000 : 26_000,
      expiresAt: (value) => value.expires_at_ms,
      started, scheduled, failed,
      onStart: () => { if (throwStart) throw new Error("private callback detail"); },
      onLifecycle: (row) => events.push(row),
      startToleranceMs: 250,
    });
    if (!throwSchedule && ok) {
      nowMs = 1_000 + offsetMs;
      callback();
    }
    return { ok, events };
  }
  const onTime = scheduleCase(250);
  assert.equal(onTime.ok, true);
  assert(onTime.events.some((row) => row.status === "started" && row.lagMs === 250));
  const late = scheduleCase(251);
  assert(late.events.some((row) => row.status === "skipped" && row.reason === "late" && row.lagMs === 251));
  const stale = scheduleCase(0, { stale: true });
  assert(stale.events.some((row) => row.status === "skipped" && row.reason === "stale"));
  const setupFailure = scheduleCase(0, { throwStart: true });
  assert(setupFailure.events.some((row) => row.status === "failed" && row.reason === "start_error"));
  const scheduleFailure = scheduleCase(0, { throwSchedule: true });
  assert.equal(scheduleFailure.ok, false);
  assert(scheduleFailure.events.some((row) => row.status === "failed" && row.reason === "start_error"));

  async function profilePrearmCase({
    replacePlan = false, lateTimerMs = 0, slowStartMs = 0,
    hangPrepare = false, hangStart = false, cancel = false,
  } = {}) {
    const profilePlan = {
      run_id: "abcdef0123456789abcdef0123456789",
      off_start_ms: 10_000, off_end_ms: 30_000,
      on_start_ms: 35_000, on_end_ms: 55_000, expires_at_ms: 70_000,
    };
    let nowMs = 20_000;
    let currentPlan = profilePlan;
    const scheduled = new Set();
    const started = new Set();
    const failed = new Set();
    const timers = [];
    const events = [];
    const counts = { prepared: 0, started: 0, stopped: 0, closed: 0 };
    const schedule = (callback, delayMs) => {
      const timer = { callback, at: nowMs + delayMs, cancelled: false };
      timers.push(timer);
      return timer;
    };
    const flush = async () => {
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    };
    const advance = async (targetMs, lateMs = 0) => {
      for (let guard = 0; guard < 10; guard += 1) {
        timers.sort((left, right) => left.at - right.at);
        const timer = timers.find((item) => !item.cancelled && item.at <= targetMs);
        if (!timer) break;
        timer.cancelled = true;
        nowMs = timer.at + (timer.at === profilePlan.on_start_ms ? lateMs : 0);
        timer.callback();
        await flush();
      }
      nowMs = Math.max(nowMs, targetMs);
      await flush();
    };
    const profileOptions = {
      now: () => nowMs,
      schedule,
      cancelTimer: (timer) => { timer.cancelled = true; },
      readPlan: () => currentPlan,
      samePlan: (expected, actual) => actual !== null
        && JSON.stringify(expected) === JSON.stringify(actual),
      phaseAt: (_current, atMs) => atMs >= 35_000 && atMs < 55_000 ? "on" : null,
      prepare: async (_plan, retainSession) => {
        counts.prepared += 1;
        const session = { id: "session" };
        retainSession(session);
        return hangPrepare ? new Promise(() => {}) : session;
      },
      start: async () => {
        counts.started += 1;
        return hangStart ? new Promise(() => {}) : nowMs + slowStartMs;
      },
      stop: async () => { counts.stopped += 1; },
      close: async () => { counts.closed += 1; },
      scheduled, started, failed,
      onStarted: (_session, _startedAtMs, _activeController) => {},
      onLifecycle: (event) => events.push(event),
      startToleranceMs: 250,
    };
    const controller = scheduler.scheduleWorkspaceCpuProfilePrearm(profilePlan, profileOptions);
    assert(controller);
    assert.equal(scheduler.scheduleWorkspaceCpuProfilePrearm(profilePlan, profileOptions), null);
    if (hangPrepare) {
      await advance(30_000);
      assert.equal(counts.prepared, 1);
      await advance(35_000);
      return { counts, events, failed, controller };
    }
    if (cancel) {
      await advance(30_000);
      assert.equal(counts.prepared, 1);
      await controller.cancel();
      await advance(35_000);
      return { counts, events, failed, controller };
    }
    await advance(30_000);
    assert.equal(counts.prepared, 1);
    if (replacePlan) currentPlan = { ...profilePlan, expires_at_ms: profilePlan.expires_at_ms + 1 };
    await advance(35_000, lateTimerMs);
    if (hangStart) await advance(35_251);
    if (counts.started === 1 && !failed.has(profilePlan.run_id)) {
      await controller.complete();
    }
    return { counts, events, failed, controller };
  }
  const profileOnTime = await profilePrearmCase();
  assert.equal(profileOnTime.counts.prepared, 1);
  assert.equal(profileOnTime.counts.started, 1);
  assert.equal(profileOnTime.counts.stopped, 0);
  assert.equal(profileOnTime.counts.closed, 1);
  assert(profileOnTime.events.some((row) => row.status === "started" && row.lagMs === 0));
  const replacedProfilePlan = await profilePrearmCase({ replacePlan: true });
  assert.equal(replacedProfilePlan.counts.started, 0);
  assert.equal(replacedProfilePlan.counts.closed, 1);
  assert(replacedProfilePlan.events.some((row) => row.status === "skipped" && row.reason === "stale"));
  const lateProfileTimer = await profilePrearmCase({ lateTimerMs: 251 });
  assert.equal(lateProfileTimer.counts.started, 0);
  assert(lateProfileTimer.events.some((row) => row.status === "skipped" && row.reason === "late" && row.lagMs === 251));
  const slowProfileStart = await profilePrearmCase({ slowStartMs: 251 });
  assert.equal(slowProfileStart.counts.started, 1);
  assert.equal(slowProfileStart.counts.stopped, 1);
  assert.equal(slowProfileStart.counts.closed, 1);
  assert(slowProfileStart.events.some((row) => row.status === "skipped" && row.reason === "late" && row.lagMs === 251));
  const hangingProfileSetup = await profilePrearmCase({ hangPrepare: true });
  assert.equal(hangingProfileSetup.counts.started, 0);
  assert.equal(hangingProfileSetup.counts.closed, 1);
  assert(hangingProfileSetup.events.some((row) => row.status === "skipped" && row.reason === "late"));
  const hangingProfileStart = await profilePrearmCase({ hangStart: true });
  assert.equal(hangingProfileStart.counts.started, 1);
  assert.equal(hangingProfileStart.counts.stopped, 1);
  assert.equal(hangingProfileStart.counts.closed, 1);
  assert(hangingProfileStart.events.some((row) => row.status === "skipped" && row.reason === "late"));
  const cancelledProfile = await profilePrearmCase({ cancel: true });
  assert.equal(cancelledProfile.counts.prepared, 1);
  assert.equal(cancelledProfile.counts.started, 0);
  assert.equal(cancelledProfile.counts.closed, 1);
  assert(cancelledProfile.failed.has("abcdef0123456789abcdef0123456789"));

  const planSource = fs.readFileSync(path.resolve(
    __dirname, "..", "lib", "performance-diagnostic-plan.ts",
  ), "utf8");
  for (const fragment of [
    'emitSsrWebLifecycleEvent(diagnostic.plan, "none", "mode", "trace_only")',
    'emitSsrWebLifecycleEvent(diagnostic.plan, diagnostic.phase, "mode", "active")',
    'emitSsrWebLifecycleEvent(plan, phase, "memory", finalStatus',
    'sampleCount: samples.length',
    "startToleranceMs: CPU_WINDOW_TOLERANCE_MS",
    'const reason = freshPlan === null ? "stale" : "mismatch"',
    'reason: "start_error"',
    'emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "failed"',
  ]) {
    if (!planSource.includes(fragment)) throw new Error(`Web lifecycle contract omitted ${fragment}`);
  }
  const usageStart = planSource.indexOf("function startCpuUsageWindow(");
  const usageEnd = planSource.indexOf("function cpuUsageWindowKey(", usageStart);
  const usageSource = planSource.slice(usageStart, usageEnd);
  if (usageStart < 0 || usageEnd <= usageStart
      || usageSource.indexOf("const startCpu = process.cpuUsage();") < 0
      || usageSource.indexOf('emitSsrWebLifecycleEvent(plan, phase, "cpu_window", "started"')
        <= usageSource.indexOf("const startCpu = process.cpuUsage();")
      || usageSource.indexOf('emitSsrWebLifecycleEvent(plan, phase, "cpu_window", finalStatus')
        <= usageSource.indexOf("const timingComplete = Boolean(")) {
    throw new Error("CPU window lifecycle timestamps do not bracket the measurement");
  }
  const profileStart = planSource.indexOf("async function startNodeCpuProfile(");
  const profileStop = planSource.indexOf("async function stopNodeCpuProfile(", profileStart);
  const profileSource = planSource.slice(profileStart, profileStop);
  if (profileStart < 0 || profileStop <= profileStart
      || profileSource.indexOf('await post<InspectorResponse<unknown>>(session, "Profiler.start")') < 0
      || profileSource.indexOf('emitSsrWebLifecycleEvent(plan, "on", "cpu_profile", "started"')
        <= profileSource.indexOf('await post<InspectorResponse<unknown>>(session, "Profiler.start")')) {
    throw new Error("CPU profile lifecycle start precedes successful Profiler.start");
  }
  const webProfilePrearm = planSource.indexOf("function scheduleWebCpuProfile(");
  const profilePrearmEnd = planSource.indexOf("function post<", webProfilePrearm);
  const webProfileSource = planSource.slice(webProfilePrearm, profilePrearmEnd);
  if (webProfilePrearm < 0 || profilePrearmEnd <= webProfilePrearm
      || !webProfileSource.includes("scheduleWorkspaceCpuProfilePrearm(plan")
      || !webProfileSource.includes("readPlan: safeReadPlan")
      || !webProfileSource.includes('"Profiler.enable"')
      || !webProfileSource.includes('"Profiler.setSamplingInterval"')
      || webProfileSource.indexOf('"Profiler.start"') < 0
      || webProfileSource.indexOf('"Profiler.start"') < webProfileSource.indexOf('"Profiler.setSamplingInterval"')
      || !webProfileSource.includes("startToleranceMs: CPU_WINDOW_TOLERANCE_MS")) {
    throw new Error("Web V8 prearm does not prepare at OFF end and start under the unchanged exact-plan guard");
  }
  const profileStopSource = planSource.slice(profileStop);
  if (!profileStopSource.includes('emitSsrWebLifecycleEvent({ run_id: runId }, "on", "cpu_profile", finalStatus')
      || !profileStopSource.includes('finalReason = "profile_bounds"')) {
    throw new Error("CPU profile lifecycle terminal status is not captured");
  }
}

function withTimeout(promise, timeoutMs) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function signalProcessGroup(child, signal) {
  if (child.exitCode !== null || child.signalCode !== null || !child.pid) {
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error && error.code !== "ESRCH") {
      throw error;
    }
  }
}

function fixtureSource({ disabled, diagnosticMarker, planSelection }) {
  if (planSelection === "capacity-close") return capacityCloseFixtureSource();
  if (planSelection === "duplicate-id-close") return duplicateSelectionCloseFixtureSource();
  const planRunId = "0123456789abcdef0123456789abcdef";
  const requestId = planSelection === "valid"
    ? "00000000000000000000000000000002"
    : planSelection === "unsampled"
      ? "11111111111111111111111111111111"
      : "d".repeat(32);
  const captureEligible = (process.getBuiltinModule("node:crypto")
    .createHash("sha256")
    .update(`${planRunId}:${requestId}`, "ascii")
    .digest()[0] & 7) === 0;
  const requestMarker = planSelection ? planRunId : (diagnosticMarker ? "1" : "");
  const registration = planSelection === "valid" || planSelection === "unsampled"
    ? `const selectorExpiresAtMs = Date.now() + 10_000;
  const selectorCaptureEligible = ${captureEligible};
  if (!selectPlanRequest({requestId: "${requestId}", runId: "${planRunId}", expiresAtMs: selectorExpiresAtMs, captureEligible: selectorCaptureEligible})) {
    const expectedCaptureEligible = (process.getBuiltinModule("node:crypto")
      .createHash("sha256")
      .update("${planRunId}:${requestId}", "ascii")
      .digest()[0] & 7) === 0;
    const now = Date.now();
    throw new Error("validated request was not selected: " + JSON.stringify({
      traceCandidate: /^[0-9a-f]{32}$/.test(request.headers["x-platform-ssr-trace"] || ""),
      traceRunMatches: request.headers["x-platform-ssr-trace"] === "${planRunId}",
      requestIdMatches: request.headers["x-request-id"] === "${requestId}",
      expiryWithinBounds: Number.isSafeInteger(selectorExpiresAtMs)
        && selectorExpiresAtMs > now && selectorExpiresAtMs - now <= 160_000,
      captureEligibilityMatches: selectorCaptureEligible === expectedCaptureEligible,
      requestStartSet: Boolean(request[Symbol.for("old-sparky.ssr-request-start")]),
    }));
  }`
    : planSelection === "wrong-request"
      ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${"e".repeat(32)}", runId: "${planRunId}", expiresAtMs: Date.now() + 10_000});`
      : planSelection === "wrong-run"
        ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${requestId}", runId: "${"f".repeat(32)}", expiresAtMs: Date.now() + 10_000});`
      : planSelection === "expired"
        ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${requestId}", runId: "${planRunId}", expiresAtMs: Date.now() - 1});`
        : "";
  return `
const http = process.getBuiltinModule("node:http");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
for (const rejected of [
  {requestId: "bad request", runId: "${planRunId}", expiresAtMs: Date.now() + 1_000},
  {requestId: "request-unselected", runId: "bad", expiresAtMs: Date.now() + 1_000},
  {requestId: "request-unselected", runId: "${planRunId}", expiresAtMs: Date.now() + 160_001},
]) {
  if (selectPlanRequest(rejected) !== false) throw new Error("stream selector accepted unbounded or malformed selection");
}
const watchdog = setTimeout(() => {
  console.error("SSR diagnostics fixture watchdog elapsed");
  process.exit(124);
}, ${fixtureWatchdogMs});
const server = http.createServer((request, response) => {
  if (request.url !== "/tournaments/private-slug") {
    response.writeHead(404);
    response.end();
    return;
  }
  ${registration}
  console.log("request_start_marker=" + (request[Symbol.for("old-sparky.ssr-request-start")] ? "set" : "none"));
  response.writeHead(200, {"content-type": "text/html"});
  ${disabled ? 'response.end("<html></html>");' : 'response.write("<html>"); response.end("</html>");'}
});
server.on("error", (error) => {
  console.error(error);
  clearTimeout(watchdog);
  process.exitCode = 1;
});
server.listen(0, "127.0.0.1", () => {
  const port = server.address().port;
  fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
    headers: {
      accept: "text/html",
      "x-request-id": "${planSelection ? requestId : disabled ? "request-disabled" : "request-1"}",
      "cf-ray": "ray-1",
      ${diagnosticMarker || planSelection ? [
        '"x-platform-timeout-diagnostic-id": "tdiag-123-00001",',
        '"x-platform-ssr-trace": "' + requestMarker + '",',
        '"x-platform-ssr-request-id": "forged-request-id",',
        '"x-platform-ssr-cf-ray": "forged-cf-ray",',
        '"x-platform-ssr-proxy-start-ms": "1",',
        '"x-platform-ssr-request-start-ms": "1",',
      ].join("\n      ") : ""}
    },
  }).then((response) => {
    console.log("client_join=" + (response.headers.get("x-platform-ssr-request-id") || "none"));
    return response.text();
  }).then(() => {
    server.close((error) => {
      clearTimeout(watchdog);
      if (error) {
        console.error(error);
        process.exitCode = 1;
      }
    });
  }).catch((error) => {
    console.error(error);
    clearTimeout(watchdog);
    process.exitCode = 1;
    server.close();
  });
});
`;
}

async function runFixture(name, {
  disabled = false,
  diagnosticMarker = false,
  planSelection = null,
  sampleRate = "1",
} = {}) {
  const environment = { ...process.env };
  if (disabled) {
    delete environment.PLATFORM_SSR_PERF_LOG_ENABLED;
  } else {
    environment.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
    environment.PLATFORM_SSR_PERF_SAMPLE_RATE = sampleRate;
  }
  const child = childProcess.spawn(
    process.execPath,
    ["--require", guardPath, "-e", fixtureSource({ disabled, diagnosticMarker, planSelection })],
    {
      detached: true,
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  const exit = new Promise((resolve) => {
    child.once("error", (error) => resolve({ kind: "error", error }));
    child.once("close", (code, signal) => resolve({ kind: "close", code, signal }));
  });

  let failure = null;
  try {
    const result = await withTimeout(exit, behaviorTimeoutMs);
    if (result.kind === "timeout") {
      failure = `SSR diagnostics ${name} fixture exceeded behavior deadline (${behaviorTimeoutMs} ms)`;
    } else if (result.kind === "error") {
      failure = `SSR diagnostics ${name} fixture failed to start: ${result.error}`;
    } else if (result.code !== 0) {
      failure = `SSR diagnostics ${name} fixture exited with code ${result.code ?? "null"}`;
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      signalProcessGroup(child, "SIGKILL");
      const cleanup = await withTimeout(exit, cleanupTimeoutMs);
      if (cleanup.kind === "timeout" && !failure) {
        failure = `SSR diagnostics ${name} fixture cleanup exceeded deadline (${cleanupTimeoutMs} ms)`;
      }
    }
  }

  if (failure) {
    throw new Error(
      `${failure}; stdout=${JSON.stringify(bounded(stdout))} stderr=${JSON.stringify(bounded(stderr))}`,
    );
  }
  return stdout;
}

function assertCount(output, fragment, expected, name) {
  const actual = output.split(fragment).length - 1;
  if (actual !== expected) {
    throw new Error(`${name}: expected ${fragment} ${expected} time(s), got ${actual}`);
  }
}

async function verifyCpuProfileSourceMapContract() {
  const { categoryForFrame, functionIdForFrame, summarizeFunctionSamples } = await import(
    path.resolve(__dirname, "../lib/cpu-profile-categories.mjs")
  );
  const { CPU_PROFILE_SOURCE_MAP_LIMITS, mapSampledFrames } = await import(
    path.resolve(__dirname, "../lib/cpu-profile-source-maps.mjs")
  );
  const { mapAfterCpuWindowSnapshot, runCpuWindowSetup } = await import(
    path.resolve(__dirname, "../lib/cpu-profile-window.mjs")
  );

  let releaseSnapshot;
  const snapshot = new Promise((resolve) => { releaseSnapshot = resolve; });
  let mapCalls = 0;
  const result = mapAfterCpuWindowSnapshot(1234, snapshot, () => {
    mapCalls += 1;
    return "mapped";
  });
  assert.equal(mapCalls, 0);
  releaseSnapshot();
  assert.deepEqual(await result, { elapsedMs: 1234, mapped: "mapped" });
  assert.equal(mapCalls, 1);

  let releaseFailedSetup;
  const failedSetupSnapshot = new Promise((resolve) => { releaseFailedSetup = resolve; });
  let settled = false;
  void failedSetupSnapshot.then(() => { settled = true; });
  const setupError = new Error("timer setup failed");
  assert.throws(() => runCpuWindowSetup(releaseFailedSetup, () => { throw setupError; }), /timer setup failed/);
  await Promise.resolve();
  assert.equal(settled, true);

  const planPath = path.resolve(__dirname, "../lib/performance-diagnostic-plan.ts");
  const planSource = fs.readFileSync(planPath, "utf8");
  const stopped = planSource.indexOf('"Profiler.stop"');
  const elapsed = planSource.indexOf("const elapsedAtStopMs = Math.max", stopped);
  const snapshotWait = planSource.indexOf('waitForCpuUsageWindowSnapshot(runId, "on")', elapsed);
  const mapping = planSource.indexOf("mapSampledFrames(", snapshotWait);
  if (stopped < 0 || elapsed <= stopped || snapshotWait <= elapsed || mapping <= snapshotWait) {
    throw new Error("Web profile maps before the process-CPU snapshot or changes stop-time elapsed accounting");
  }
  if (!planSource.includes("Math.max(0, endTarget - startMs)")
      || !planSource.includes("const usage = process.cpuUsage(startCpu);")) {
    throw new Error("Web process-CPU window timing inputs changed");
  }

  const root = fs.mkdtempSync(path.join(os.tmpdir(), "web-cpu-map-"));
  const appRoot = path.join(root, "app");
  const runtimeCwd = path.join(appRoot, ".next", "standalone");
  const runtimeChunks = path.join(runtimeCwd, ".next", "server", "chunks");
  const siblingChunks = path.join(appRoot, ".next", "server", "chunks");
  fs.mkdirSync(runtimeChunks, { recursive: true });
  fs.mkdirSync(siblingChunks, { recursive: true });
  const addChunk = (name, sourceUrl) => {
    const runtimePath = path.join(runtimeChunks, name);
    const siblingPath = path.join(siblingChunks, name);
    const code = "function compiled() { return 1; }\n";
    fs.writeFileSync(runtimePath, code);
    fs.writeFileSync(siblingPath, code);
    fs.writeFileSync(`${siblingPath}.map`, JSON.stringify({
      version: 3, sources: [sourceUrl], names: [], mappings: "AAAA",
    }));
    return runtimePath;
  };
  try {
    const nextPath = addChunk("next.js", path.join(root, "node_modules/next/dist/server/render.js"));
    const reactPath = addChunk("react.js", path.join(root, "node_modules/react-dom/server.js"));
    const oddPath = addChunk("odd.js", path.join(root, "vendor/unknown.js"));
    const indexedPath = addChunk("indexed.js", path.join(root, "node_modules/next/dist/server/response.js"));
    fs.writeFileSync(`${path.join(siblingChunks, "indexed.js")}.map`, JSON.stringify({
      version: 3,
      sections: [{
        offset: { line: 0, column: 0 },
        map: { version: 3, sources: [path.join(root, "node_modules/next/dist/server/response.js")], names: [], mappings: "AAAA" },
      }],
    }));
    const linkedPath = path.join(runtimeChunks, "linked.js");
    fs.symlinkSync(nextPath, linkedPath);
    fs.writeFileSync(path.join(siblingChunks, "linked.js"), fs.readFileSync(nextPath));
    fs.writeFileSync(`${path.join(siblingChunks, "linked.js")}.map`, fs.readFileSync(`${path.join(siblingChunks, "next.js")}.map`));
    const pathToFileURL = process.getBuiltinModule("node:url").pathToFileURL;
    const raw = new Map([
      [1, { url: nextPath, functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
      [2, { url: reactPath, functionName: "renderToString", lineNumber: 0, columnNumber: 0 }],
      [3, { url: nextPath, functionName: "renderToResponseWithComponentsImpl" }],
      [4, { url: "webpack:///node_modules/next/dist/server/render.js", functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
      [5, { url: oddPath, functionName: "privateSymbol", lineNumber: 0, columnNumber: 0 }],
      [6, { url: path.join(root, "outside.js"), functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
      [7, { url: nextPath, functionName: "unknownFrameworkName", lineNumber: 0, columnNumber: 0 }],
      [8, { url: pathToFileURL(nextPath).href, functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
      [9, { url: linkedPath, functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
      [10, { url: indexedPath, functionName: "renderToResponseWithComponentsImpl", lineNumber: 0, columnNumber: 0 }],
    ]);
    const mapped = mapSampledFrames(raw, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], runtimeCwd);
    assert.equal(categoryForFrame(mapped.get(1)?.url ?? "", mapped.get(1)?.functionName), "web_framework");
    assert.equal(functionIdForFrame(mapped.get(1)?.url ?? "", mapped.get(1)?.functionName), "web.next.render_response");
    assert.equal(functionIdForFrame(mapped.get(2)?.url ?? "", mapped.get(2)?.functionName), "web.react.render_string");
    assert.deepEqual(mapped.get(3), raw.get(3));
    assert.deepEqual(mapped.get(4), raw.get(4));
    assert.equal(categoryForFrame(mapped.get(5)?.url ?? "", mapped.get(5)?.functionName), "other");
    assert.equal(functionIdForFrame(mapped.get(5)?.url ?? "", mapped.get(5)?.functionName), null);
    assert.deepEqual(mapped.get(6), raw.get(6));
    assert.equal(categoryForFrame(mapped.get(7)?.url ?? "", mapped.get(7)?.functionName), "web_framework");
    assert.equal(functionIdForFrame(mapped.get(7)?.url ?? "", mapped.get(7)?.functionName), null);
    assert.equal(functionIdForFrame(mapped.get(8)?.url ?? "", mapped.get(8)?.functionName), "web.next.render_response");
    assert.deepEqual(mapped.get(9), raw.get(9));
    assert.equal(functionIdForFrame(mapped.get(10)?.url ?? "", mapped.get(10)?.functionName), "web.next.render_response");
    const summary = summarizeFunctionSamples(mapped, [1, 2, 1]);
    assert.deepEqual(summary.rows, [
      { function_id: "web.next.render_response", samples: 2 },
      { function_id: "web.react.render_string", samples: 1 },
    ]);
    assert.doesNotMatch(JSON.stringify(summary), /web-cpu-map|render\.js|renderToResponse/);
    assert.deepEqual(CPU_PROFILE_SOURCE_MAP_LIMITS, {
      files: 192, mapBytes: 2 * 1024 * 1024, totalBytes: 12 * 1024 * 1024, generatedBytes: 16 * 1024 * 1024,
    });

    const mapPath = `${path.join(siblingChunks, "next.js")}.map`;
    fs.writeFileSync(mapPath, "not json");
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
    fs.writeFileSync(path.join(siblingChunks, "next.js"), "mismatched generated code");
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
    fs.writeFileSync(path.join(siblingChunks, "next.js"), fs.readFileSync(nextPath));
    fs.rmSync(mapPath);
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
    fs.symlinkSync(path.join(root, "elsewhere.map"), mapPath);
    fs.writeFileSync(path.join(root, "elsewhere.map"), JSON.stringify({ version: 3, sources: ["node:internal/x"], names: [], mappings: "AAAA" }));
    assert.equal(fs.lstatSync(mapPath).isSymbolicLink(), true);
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
    fs.rmSync(mapPath);
    for (const limits of [
      { ...CPU_PROFILE_SOURCE_MAP_LIMITS, files: 0 },
      { ...CPU_PROFILE_SOURCE_MAP_LIMITS, mapBytes: 1 },
      { ...CPU_PROFILE_SOURCE_MAP_LIMITS, totalBytes: 1 },
      { ...CPU_PROFILE_SOURCE_MAP_LIMITS, generatedBytes: 1 },
    ]) {
      assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd, limits).get(1), raw.get(1));
    }
    fs.mkdirSync(mapPath);
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
    fs.rmSync(mapPath, { recursive: true });
    fs.writeFileSync(mapPath, JSON.stringify({
      version: 3, sources: [path.join(root, "node_modules/next/dist/server/render.js")], names: [], mappings: "",
    }));
    assert.deepEqual(mapSampledFrames(raw, [1], runtimeCwd).get(1), raw.get(1));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function main() {
  await verifySsrWebLifecycleContract();
  await verifyCpuProfileSourceMapContract();
  const guardSource = fs.readFileSync(guardPath, "utf8");
  for (const fragment of [
    "maxSelectedRequests = 128",
    "maxPendingRequests = 128",
    "maxRowsPerRequest = 6",
    "maxRowsPerPlan = 6_000",
    'captureSampleScheme = "sha256-runid-requestid-1of8-v1"',
    '(digest[0] & 7) === 0',
    "captureEligible: entry.captureEligible",
    "ssr_stream_capture_coverage scheme=${captureSampleScheme}",
    "selector_overflow_unattributed=",
    'setHeader("x-platform-ssr-request-id", entry.requestId)',
  ]) {
    if (!guardSource.includes(fragment)) {
      throw new Error(`SSR stream guard contract omitted ${fragment}`);
    }
  }
  const prearmModule = await import(path.resolve(
    __dirname, "..", "lib", "workspace-diagnostic-prearm.mjs",
  ));
  const pagePath = path.resolve(
    __dirname, "..", "app", "(site)", "tournaments", "[slug]", "page.tsx",
  );
  const planPath = path.resolve(__dirname, "..", "lib", "performance-diagnostic-plan.ts");
  const proxyPath = path.resolve(__dirname, "..", "proxy.ts");
  const pageSource = fs.readFileSync(pagePath, "utf8");
  const planSource = fs.readFileSync(planPath, "utf8");
  const proxySource = fs.readFileSync(proxyPath, "utf8");
  const { isHtmlSsrTraceDrainEligible } = await import(path.resolve(
    __dirname, "..", "lib", "ssr-capture-sampling.mjs",
  ));
  const onStartMs = 126_000;
  const onEndMs = 146_000;
  const expiresAtMs = 161_000;
  for (const workload of [
    "authenticated_html_ssr_capacity_v1",
    "authenticated_html_ssr_capacity_120s_v1",
    "authenticated_html_ssr_cpu_profile_20s_v1",
  ]) {
    for (const [parentStartMs, nowMs] of [
      [onStartMs - 28_000, onStartMs - 28_000],
      [onStartMs - 24_000, onStartMs - 24_000],
      [onStartMs, onEndMs],
      [onEndMs - 1, onEndMs + 1],
      [onEndMs, onEndMs],
      [onEndMs + 10, onEndMs + 11],
      [expiresAtMs, expiresAtMs],
    ]) {
      if (!isHtmlSsrTraceDrainEligible(workload, parentStartMs, nowMs, onStartMs, onEndMs, expiresAtMs)) {
        throw new Error(`${workload}: trusted receipt inside the bounded trace drain was rejected`);
      }
    }
  }
  for (const [label, workload, parentStartMs, nowMs] of [
    ["future parent", "authenticated_html_ssr_capacity_120s_v1", onEndMs + 2, onEndMs + 1],
    ["before trace floor", "authenticated_html_ssr_capacity_120s_v1", onStartMs - 28_001, onStartMs],
    ["expired plan", "authenticated_html_ssr_capacity_120s_v1", onEndMs - 1, expiresAtMs + 1],
    ["post-expiry receipt", "authenticated_html_ssr_cpu_profile_20s_v1", expiresAtMs + 1, expiresAtMs + 1],
    ["oversized drain", "authenticated_html_ssr_capacity_120s_v1", onEndMs - 1, onEndMs + 15_001],
    ["wrong workload", "authenticated_workspace_read_capacity_v1", onEndMs - 1, onEndMs + 1],
    ["invalid parent time", "authenticated_html_ssr_capacity_120s_v1", Number.NaN, onEndMs + 1],
  ]) {
    if (isHtmlSsrTraceDrainEligible(workload, parentStartMs, nowMs, onStartMs, onEndMs, expiresAtMs)) {
      throw new Error(`trace drain accepted ${label}`);
    }
  }
  if (isHtmlSsrTraceDrainEligible(
    "authenticated_html_ssr_capacity_120s_v1", onEndMs + 1, onEndMs + 2,
    onStartMs, onEndMs, onEndMs + 15_001,
  )) {
    throw new Error("trace drain accepted an expiry longer than the fixed 15-second grace");
  }
  const proxyHeaderClear = proxySource.indexOf("requestHeaders.delete(SSR_REQUEST_START_HEADER);");
  const proxyHeaderStamp = proxySource.indexOf("requestHeaders.set(SSR_REQUEST_START_HEADER, String(proxyStartMs));");
  if (proxyHeaderClear < 0 || proxyHeaderStamp <= proxyHeaderClear
      || !proxySource.includes("const proxyStartMs = Date.now();")
      || !planSource.includes("const parentStartText = requestHeaders.get(SSR_REQUEST_START_HEADER) ?? \"\";")
      || !planSource.includes("hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)")) {
    throw new Error("trace drain must use the server proxy's caller-overwritten parent-start");
  }
  const validatedStart = planSource.indexOf("function validatedSsrDiagnostic(");
  const activePhaseBranch = planSource.indexOf("if (phase !== null) {", validatedStart);
  const drainCheck = planSource.indexOf("isHtmlSsrTraceDrainEligible(", validatedStart);
  const traceOnlyFallback = planSource.indexOf("if (!traceEligible) return null;", validatedStart);
  const runStart = planSource.indexOf("export async function runWithSsrDiagnosticPlan(");
  const traceOnlyReturn = planSource.indexOf("if (diagnostic.traceOnly === true)", runStart);
  const usageStart = planSource.indexOf("startCpuUsageWindow(diagnostic.plan, diagnostic.phase);", runStart);
  const memoryStart = planSource.indexOf("startNodeMemoryWindow(diagnostic.plan, diagnostic.phase);", runStart);
  const profileStart = planSource.indexOf("await startAuthorizedNodeCpuProfile(diagnostic.plan);", runStart);
  const runIdReader = planSource.indexOf("export function current_diagnostic_run_id(): string | null {");
  if (validatedStart < 0 || drainCheck <= validatedStart
      || activePhaseBranch <= drainCheck || activePhaseBranch >= traceOnlyFallback
      || !planSource.includes("return { plan, phase, ...(traceEligible ? { parentStartMs: parentStartMs! } : {}) };")
      || traceOnlyReturn <= runStart || usageStart <= traceOnlyReturn
      || memoryStart <= traceOnlyReturn || profileStart <= traceOnlyReturn
      || runIdReader < 0
      || planSource.indexOf("if (activePhase(current.plan, now) === current.phase)", runIdReader) < 0
      || planSource.indexOf("isHtmlSsrTraceDrainEligible(", runIdReader) < 0
      || !planSource.includes("return diagnosticStorage.run(diagnostic, operation);")) {
    throw new Error("trace-only drain is not separated from capture-window startup");
  }
  const baseHeaders = { cookie: "session=fixture" };
  const runId = "a".repeat(32);
  const prearmPlan = {
    run_id: runId,
    workload: "authenticated_html_ssr_capacity_v1",
    off_start_ms: 100_000,
  };
  const authorizedRunId = prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
    prearmPlan, { ...prearmPlan }, 48_000,
  );
  for (const workload of [
    "authenticated_html_ssr_capacity_120s_v1",
    "authenticated_html_ssr_cpu_profile_20s_v1",
  ]) {
    const typedPlan = { ...prearmPlan, workload };
    if (prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
      typedPlan, { ...typedPlan }, 48_000,
    ) !== runId) {
      throw new Error(`workspace prearm rejected the approved typed HTML workload ${workload}`);
    }
  }
  let prearmPlanReadCount = 0;
  const absentContextRunId = prearmModule.readAuthorizedWorkspaceDiagnosticPrearmRunId(
    null,
    () => {
      prearmPlanReadCount += 1;
      return prearmPlan;
    },
    48_000,
  );
  const boundContextRunId = prearmModule.readAuthorizedWorkspaceDiagnosticPrearmRunId(
    prearmPlan,
    () => {
      prearmPlanReadCount += 1;
      return { ...prearmPlan };
    },
    48_000,
  );
  if (absentContextRunId !== null || boundContextRunId !== runId || prearmPlanReadCount !== 1) {
    throw new Error("workspace prearm plan revalidation read without context or skipped bound context");
  }
  const sampledCorrelationHeaders = {
    ...baseHeaders,
    "x-platform-ssr-trace": "1",
    "x-platform-ssr-request-id": "request-fixture",
  };
  const forwarded = prearmModule.withWorkspaceDiagnosticPrearmHeader(
    sampledCorrelationHeaders, authorizedRunId,
  );
  if (forwarded.cookie !== baseHeaders.cookie
      || forwarded["x-platform-ssr-trace"] !== runId
      || forwarded["x-platform-ssr-request-id"] !== "request-fixture"
      || Object.hasOwn(baseHeaders, "x-platform-ssr-trace")) {
    throw new Error("workspace prearm helper did not retain the exact marker after correlation headers");
  }
  const correlationWithoutPrearm = prearmModule.withWorkspaceDiagnosticPrearmHeader(
    sampledCorrelationHeaders, null,
  );
  if (correlationWithoutPrearm["x-platform-ssr-trace"] !== "1") {
    throw new Error("workspace prearm helper changed a sampled correlation marker without a plan");
  }
  for (const untrusted of [null, "1", `${runId}extra`, runId.toUpperCase()]) {
    const rejected = prearmModule.withWorkspaceDiagnosticPrearmHeader(baseHeaders, untrusted);
    if (Object.hasOwn(rejected, "x-platform-ssr-trace")) {
      throw new Error("workspace prearm helper forwarded an absent or malformed marker");
    }
  }
  for (const [currentPlan, nowMs] of [
    [null, 48_000],
    [{ ...prearmPlan, source_sha: "b".repeat(40) }, 48_000],
    [{ ...prearmPlan, workload: "authenticated_workspace_read_pair_v1" }, 48_000],
    [{ ...prearmPlan }, 100_000],
    [{ ...prearmPlan }, 47_999],
  ]) {
    if (prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
      prearmPlan, currentPlan, nowMs,
    ) !== null) {
      throw new Error("workspace prearm authorization accepted absent, replaced, wrong-scope or mistimed plan");
    }
  }
  if (!pageSource.includes("withWorkspaceDiagnosticPrearmHeader(")
      || !pageSource.includes("current_workspace_cpu_diagnostic_prearm_run_id()")) {
    throw new Error("tournament page no longer uses the validated workspace prearm helper");
  }
  const correlationMerge = pageSource.indexOf("getServerRequestCorrelationHeaders()).forEach");
  const prearmMerge = pageSource.indexOf(
    "withWorkspaceDiagnosticPrearmHeader(", pageSource.indexOf("const workspaceHeaders"),
  );
  if (correlationMerge < 0 || prearmMerge <= correlationMerge) {
    throw new Error("validated workspace prearm marker must be merged after sampled correlation headers");
  }
  for (const fragment of [
    "diagnosticPrearmStorage",
    "hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)",
    "authorizedWorkspaceDiagnosticPrearmRunId(",
    "readAuthorizedWorkspaceDiagnosticPrearmRunId(",
  ]) {
    if (!planSource.includes(fragment)) {
      throw new Error(`workspace prearm plan contract omitted ${fragment}`);
    }
  }
  const prearmGetter = planSource.match(
    /export function current_workspace_cpu_diagnostic_prearm_run_id\(\): string \| null \{([\s\S]*?)\n\}/u,
  );
  if (!prearmGetter
      || !prearmGetter[1].includes("prearmPlan ?? null, safeReadPlan, Date.now()")
      || prearmGetter[1].includes("safeReadPlan()")) {
    throw new Error("workspace prearm getter must defer plan reads until its dedicated context exists");
  }

  const enabledOutput = await runFixture("enabled");
  for (const stage of [
    "response_stream_start",
    "first_body_write_attempt",
    "response_finish",
    "response_close",
  ]) {
    assertCount(enabledOutput, `stage=${stage}`, 1, "enabled SSR diagnostics");
  }
  if (enabledOutput.includes("stage=response_error")) {
    throw new Error("enabled SSR diagnostics emitted response_error");
  }
  for (const fragment of [
    "writable_finished=0",
    "writable_finished=1",
    "write_count=2",
    "body_bytes=13",
    "request_id=request-1",
    "cf_ray=ray-1",
  ]) {
    if (!enabledOutput.includes(fragment)) {
      throw new Error(`enabled SSR diagnostics omitted ${fragment}`);
    }
  }

  const disabledOutput = await runFixture("disabled", { disabled: true });
  if (disabledOutput.includes("ssr_stream")
      || !disabledOutput.includes("client_join=none")
      || !disabledOutput.includes("request_start_marker=none")) {
    throw new Error("disabled SSR diagnostics emitted a stream log");
  }

  const untrustedHeadersOutput = await runFixture("public-diagnostic-headers", {
    diagnosticMarker: true,
    sampleRate: "0",
  });
  if (untrustedHeadersOutput.includes("ssr_stream")
      || !untrustedHeadersOutput.includes("client_join=none")) {
    throw new Error("public diagnostic headers promoted an unsampled SSR stream");
  }

  const validatedPlanOutput = await runFixture("validated-plan", {
    disabled: true,
    planSelection: "valid",
  });
  for (const stage of [
    "request_start",
    "response_stream_start",
    "first_body_write_attempt",
    "response_finish",
    "response_close",
  ]) {
    assertCount(validatedPlanOutput, `stage=${stage}`, 1, "validated SSR plan");
  }
  if (!validatedPlanOutput.includes("request_id=00000000000000000000000000000002")
      || !validatedPlanOutput.includes("client_join=00000000000000000000000000000002")
      || !validatedPlanOutput.includes("request_start_marker=set")
      || validatedPlanOutput.includes("session=")
      || validatedPlanOutput.includes("private-slug")) {
    throw new Error("validated plan stream rows lost correlation or exposed private request data");
  }
  const unsampledPlanOutput = await runFixture("validated-plan-unsampled", {
    disabled: true,
    planSelection: "unsampled",
  });
  if (unsampledPlanOutput.includes("ssr_stream")
      || !unsampledPlanOutput.includes("client_join=11111111111111111111111111111111")
      || !unsampledPlanOutput.includes("request_start_marker=set")) {
    throw new Error("unsampled valid typed plan lost its response join or emitted stream rows");
  }
  const closedNoneligibleOutput = await runFixture("closed-noneligible-plan-capacity", {
    disabled: true,
    planSelection: "capacity-close",
  });
  if (!closedNoneligibleOutput.includes(
    "closed_noneligible=128 eligible_after_close=accepted eligible_again=accepted",
  )) {
    throw new Error("closed noneligible plan entries consumed selector capacity");
  }
  for (const fragment of [
    "plan_selected=130",
    "selector_invalid=1",
    "header_set=130",
    "header_already_sent=0",
    "header_unavailable=0",
    "outcome_status=complete",
  ]) {
    if (!closedNoneligibleOutput.includes(fragment)) {
      throw new Error(`header outcome fixture omitted ${fragment}`);
    }
  }
  const duplicateIdCloseOutput = await runFixture("duplicate-plan-id-close", {
    disabled: true,
    planSelection: "duplicate-id-close",
  });
  assertCount(duplicateIdCloseOutput, "stage=response_stream_start", 2, "duplicate-ID plan close");
  if (!duplicateIdCloseOutput.includes("duplicate_id_replacement=accepted")) {
    throw new Error("closing an older duplicate-ID response removed the newer selection");
  }
  for (const selection of ["wrong-request", "wrong-run", "expired"]) {
    const rejectedPlanOutput = await runFixture(`plan-${selection}`, {
      disabled: true,
      planSelection: selection,
    });
    if (rejectedPlanOutput.includes("ssr_stream")) {
      throw new Error(`SSR stream guard accepted ${selection} selector state`);
    }
    if (!rejectedPlanOutput.includes("client_join=none")) {
      throw new Error(`SSR stream guard set a response join header for ${selection} selector state`);
    }
  }
  const forgedPlanMarkerOutput = await runFixture("forged-plan-marker", {
    disabled: true,
    planSelection: "forged",
  });
  if (forgedPlanMarkerOutput.includes("ssr_stream")) {
    throw new Error("SSR stream guard trusted a caller marker without validated request selection");
  }
  if (!forgedPlanMarkerOutput.includes("client_join=none")) {
    throw new Error("SSR stream guard set a response join header from a caller marker alone");
  }
}

void main().catch((error) => {
  console.error(`SSR diagnostics contract failed: ${error}`);
  process.exitCode = 1;
});
