const PHASES = new Set(["off", "on"]);
const LIFECYCLE_PHASES = new Set(["off", "on", "none"]);
const LIFECYCLE_FIELDS = Object.freeze({
  mode: new Set(["active", "trace_only"]),
  cpu_window: new Set(["scheduled", "started", "skipped", "finalized", "failed"]),
  cpu_profile: new Set(["started", "skipped", "finalized", "failed"]),
  memory: new Set(["started", "skipped", "failed", "finalized"]),
});
const LIFECYCLE_REASONS = new Set([
  "none", "late", "stale", "mismatch", "start_error", "end_error",
  "timing_incomplete", "profile_bounds", "profile_stop", "already_claimed",
]);
const LIFECYCLE_EVENT_LIMIT = 256;
const LIFECYCLE_DEDUPE_TTL_MS = 600_000;
const lifecycleEvents = new Map();

/** Best-effort private lifecycle row with a closed field vocabulary and bounded dedupe state. */
export function emitSsrWebLifecycleEvent(
  plan,
  phase,
  event,
  status,
  { reason = "none", lagMs = null, sampleCount = null, nowMs = Date.now(), writeLine } = {},
) {
  try {
    if (!plan || typeof plan.run_id !== "string" || !/^[0-9a-f]{32}$/u.test(plan.run_id)
        || !LIFECYCLE_PHASES.has(phase) || !LIFECYCLE_FIELDS[event]?.has(status)
        || !LIFECYCLE_REASONS.has(reason) || !Number.isSafeInteger(nowMs)
        || (lagMs !== null && (!Number.isSafeInteger(lagMs) || Math.abs(lagMs) > 1_000_000))
        || (sampleCount !== null && (!Number.isSafeInteger(sampleCount) || sampleCount < 0 || sampleCount > 5))) {
      return false;
    }
    for (const [key, recordedAt] of lifecycleEvents) {
      if (nowMs >= recordedAt && nowMs - recordedAt > LIFECYCLE_DEDUPE_TTL_MS) {
        lifecycleEvents.delete(key);
      }
    }
    const key = `${plan.run_id}:${phase}:${event}:${status}`;
    if (lifecycleEvents.has(key) || lifecycleEvents.size >= LIFECYCLE_EVENT_LIMIT) return false;
    lifecycleEvents.set(key, nowMs);
    const lag = lagMs === null ? "unknown" : String(lagMs);
    const count = sampleCount === null ? "unknown" : String(sampleCount);
    const line = `ssr_web_lifecycle service=web run_id=${plan.run_id} phase=${phase} `
      + `event=${event} status=${status} reason=${reason} lag_ms=${lag} sample_count=${count}`;
    (typeof writeLine === "function" ? writeLine : console.info)(line);
    return true;
  } catch {
    return false;
  }
}

function cpuWindowKey(plan, phase) {
  return `${plan.run_id}:${phase}`;
}

export function claimWorkspaceCpuWindowStart(
  plan,
  phase,
  { started, scheduled, failed },
) {
  if (!plan || typeof plan.run_id !== "string" || !PHASES.has(phase)) {
    return false;
  }
  const key = cpuWindowKey(plan, phase);
  if (started.has(key) || scheduled.has(key) || failed.has(key)) return false;
  started.add(key);
  return true;
}

export function scheduleAuthorizedWorkspaceCpuWindowEdges(
  plan,
  freshPlan,
  authorizedRunId,
  samePlan,
  scheduleWindow,
) {
  if (!plan || !freshPlan || typeof plan.run_id !== "string"
      || authorizedRunId !== plan.run_id || typeof samePlan !== "function"
      || typeof scheduleWindow !== "function") {
    return false;
  }
  let matches;
  try {
    matches = samePlan(plan, freshPlan);
  } catch {
    return false;
  }
  if (matches !== true) return false;
  scheduleWindow(freshPlan, "off");
  scheduleWindow(freshPlan, "on");
  return true;
}

export function scheduleWorkspaceCpuWindowStart(
  plan,
  phase,
  {
    now,
    schedule,
    readPlan,
    samePlan,
    phaseAt,
    startTarget,
    expiresAt,
    started,
    scheduled,
    failed,
    onStart,
    onLifecycle,
    startToleranceMs,
  },
) {
  if (!plan || typeof plan.run_id !== "string" || !PHASES.has(phase)
      || typeof now !== "function" || typeof schedule !== "function"
      || typeof readPlan !== "function" || typeof samePlan !== "function"
      || typeof phaseAt !== "function" || typeof startTarget !== "function"
      || typeof expiresAt !== "function" || typeof onStart !== "function"
      || !Number.isSafeInteger(startToleranceMs) || startToleranceMs < 0) {
    return false;
  }

  const key = cpuWindowKey(plan, phase);
  const report = (status, reason, lagMs = null) => {
    if (typeof onLifecycle !== "function") return;
    try {
      onLifecycle({ status, reason, lagMs });
    } catch {
      // Optional lifecycle reporting cannot affect the capture guard.
    }
  };
  if (started.has(key) || scheduled.has(key) || failed.has(key)) {
    report("skipped", "already_claimed");
    return false;
  }

  let targetMs;
  let nowMs;
  try {
    targetMs = startTarget(plan, phase);
    nowMs = now();
  } catch {
    failed.add(key);
    report("failed", "start_error");
    return false;
  }
  if (!Number.isSafeInteger(targetMs) || !Number.isSafeInteger(nowMs)
      || targetMs <= nowMs) {
    failed.add(key);
    report(Number.isSafeInteger(targetMs) && Number.isSafeInteger(nowMs) && targetMs <= nowMs
      ? "skipped" : "failed", Number.isSafeInteger(targetMs) && Number.isSafeInteger(nowMs)
      && targetMs <= nowMs ? "late" : "start_error",
    Number.isSafeInteger(targetMs) && Number.isSafeInteger(nowMs) ? nowMs - targetMs : null);
    return false;
  }

  scheduled.add(key);
  const failClosed = () => {
    scheduled.delete(key);
    failed.add(key);
  };

  const failReported = (reason, lagMs = null) => {
    failClosed();
    report(reason === "late" || reason === "stale" ? "skipped" : "failed", reason, lagMs);
  };

  const scheduleAtEdge = (delayMs) => {
    try {
      schedule(startAtEdge, Math.max(0, delayMs));
    } catch {
      failReported("start_error");
    }
  };

  const startAtEdge = () => {
    let actualMs;
    let current;
    try {
      actualMs = now();
      current = readPlan();
    } catch {
      failReported("start_error");
      return;
    }
    if (!Number.isSafeInteger(actualMs)) {
      failReported("start_error");
      return;
    }
    const lagMs = actualMs - targetMs;
    if (!samePlan(plan, current)) {
      failReported("stale", lagMs);
      return;
    }
    if (actualMs < targetMs) {
      scheduleAtEdge(targetMs - actualMs);
      return;
    }
    let currentTarget;
    let currentExpiry;
    let currentPhase;
    try {
      currentTarget = startTarget(current, phase);
      currentExpiry = expiresAt(current);
      currentPhase = phaseAt(current, actualMs);
    } catch {
      failReported("start_error", lagMs);
      return;
    }
    if (!Number.isSafeInteger(currentTarget) || currentTarget !== targetMs
        || !Number.isSafeInteger(currentExpiry) || actualMs > currentExpiry
        || currentPhase !== phase) {
      failReported("stale", lagMs);
      return;
    }
    if (lagMs > startToleranceMs) {
      failReported("late", lagMs);
      return;
    }
    scheduled.delete(key);
    try {
      onStart(current, phase);
    } catch {
      failed.add(key);
      report("failed", "start_error", lagMs);
    }
    if (!failed.has(key)) report("started", "none", lagMs);
  };

  scheduleAtEdge(targetMs - nowMs);
  const didSchedule = scheduled.has(key);
  if (didSchedule) report("scheduled", "none", nowMs - targetMs);
  return didSchedule;
}
