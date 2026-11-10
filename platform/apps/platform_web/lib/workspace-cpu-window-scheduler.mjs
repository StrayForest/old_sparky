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
const LIFECYCLE_PROFILE_STAGES = new Set([
  "on_request", "prearm_claim", "prepare_timer", "prepare_deadline",
  "prepare_completion", "start_timer", "start_command_deadline",
  "start_command_completion", "start_command_failure", "profile_stop",
  "prearm_validation", "prearm_cancel",
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
  {
    reason = "none", lagMs = null, sampleCount = null, stage = null,
    nowMs = Date.now(), writeLine,
  } = {},
) {
  try {
    if (!plan || typeof plan.run_id !== "string" || !/^[0-9a-f]{32}$/u.test(plan.run_id)
        || !LIFECYCLE_PHASES.has(phase) || !LIFECYCLE_FIELDS[event]?.has(status)
        || !LIFECYCLE_REASONS.has(reason) || !Number.isSafeInteger(nowMs)
        || (event === "cpu_profile"
          ? !LIFECYCLE_PROFILE_STAGES.has(stage)
          : stage !== null)
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
      + `event=${event} status=${status} reason=${reason} lag_ms=${lag} sample_count=${count}`
      + (event === "cpu_profile" ? ` stage=${stage}` : "");
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

export function scheduleWorkspaceCpuProfilePrearm(
  plan,
  {
    now,
    schedule,
    cancelTimer,
    readPlan,
    samePlan,
    phaseAt,
    prepare,
    start,
    stop,
    close,
    scheduled,
    started,
    failed,
    onStarted,
    onLifecycle,
    startToleranceMs,
  },
) {
  if (!plan || typeof plan.run_id !== "string"
      || !Number.isSafeInteger(plan.off_end_ms)
      || !Number.isSafeInteger(plan.on_start_ms)
      || !Number.isSafeInteger(plan.on_end_ms)
      || typeof now !== "function" || typeof schedule !== "function"
      || typeof cancelTimer !== "function" || typeof readPlan !== "function"
      || typeof samePlan !== "function" || typeof phaseAt !== "function"
      || typeof prepare !== "function" || typeof start !== "function"
      || typeof stop !== "function" || typeof close !== "function"
      || typeof onStarted !== "function"
      || !Number.isSafeInteger(startToleranceMs) || startToleranceMs < 0) {
    return null;
  }

  const key = plan.run_id;
  if (scheduled.has(key) || started.has(key) || failed.has(key)) return null;

  let state = "scheduled";
  let timer = null;
  let deadlineTimer = null;
  let session = null;
  let startAttempted = false;
  let closed = false;
  let stopPromise = null;
  const report = (status, reason, lagMs = null, stage = "prearm_validation") => {
    try {
      onLifecycle?.({ status, reason, lagMs, stage });
    } catch {
      // Lifecycle output cannot change the profiler guard.
    }
  };
  const closeSession = async () => {
    if (session === null || closed) return;
    closed = true;
    try {
      await close(session);
    } catch {
      // Inspector cleanup is best-effort after a fail-closed decision.
    }
  };
  const stopSession = async () => {
    if (session === null) return;
    if (stopPromise === null) {
      let timeout;
      const timeoutCall = new Promise((resolve) => {
        timeout = setTimeout(resolve, 100);
      });
      stopPromise = Promise.race([
        Promise.resolve().then(() => stop(session)).catch(() => {}),
        timeoutCall,
      ]).finally(() => clearTimeout(timeout));
    }
    await stopPromise;
  };
  const clearTimer = (handle) => {
    if (handle === null) return;
    try {
      cancelTimer(handle);
    } catch {
      // Timer cancellation is best-effort; every callback rechecks plan state.
    }
  };
  const clearTimers = () => {
    clearTimer(timer);
    clearTimer(deadlineTimer);
    timer = null;
    deadlineTimer = null;
  };
  const finishFailed = async (reason, lagMs = null, stage = "prearm_validation") => {
    if (["failed", "cancelled", "complete"].includes(state)) return;
    state = "failed";
    scheduled.delete(key);
    started.delete(key);
    failed.add(key);
    clearTimers();
    if (startAttempted) await stopSession();
    await closeSession();
    report(reason === "late" || reason === "stale" ? "skipped" : "failed", reason, lagMs, stage);
  };
  const matchingPlan = () => {
    try {
      const current = readPlan();
      return current !== null && samePlan(plan, current) === true ? current : null;
    } catch {
      return null;
    }
  };
  const scheduleAt = (targetMs, callback, stage) => {
    try {
      timer = schedule(callback, Math.max(0, targetMs - now()));
    } catch {
      void finishFailed("start_error", null, stage);
    }
  };
  const scheduleDeadlineAt = (targetMs, callback, stage) => {
    try {
      deadlineTimer = schedule(callback, Math.max(0, targetMs - now()));
    } catch {
      void finishFailed("start_error", null, stage);
    }
  };

  const startAtEdge = async () => {
    if (state !== "prepared") return;
    let actualMs;
    try {
      actualMs = now();
    } catch {
      await finishFailed("start_error", null, "start_timer");
      return;
    }
    if (!Number.isSafeInteger(actualMs)) {
      await finishFailed("start_error", null, "start_timer");
      return;
    }
    if (actualMs < plan.on_start_ms) {
      scheduleAt(plan.on_start_ms, () => { void startAtEdge(); }, "start_timer");
      return;
    }
    const lagMs = actualMs - plan.on_start_ms;
    if (lagMs > startToleranceMs) {
      await finishFailed("late", lagMs, "start_timer");
      return;
    }
    const current = matchingPlan();
    let phase;
    try {
      phase = current === null ? null : phaseAt(current, actualMs);
    } catch {
      await finishFailed("start_error", lagMs, "prearm_validation");
      return;
    }
    if (current === null || phase !== "on" || actualMs > current.expires_at_ms) {
      await finishFailed("stale", lagMs, "prearm_validation");
      return;
    }

    state = "starting";
    startAttempted = true;
    const expireSlowStart = () => {
      if (state !== "starting") return;
      let timeoutAtMs;
      try {
        timeoutAtMs = now();
      } catch {
        void finishFailed("start_error", null, "start_command_deadline");
        return;
      }
      if (!Number.isSafeInteger(timeoutAtMs)) {
        void finishFailed("start_error", null, "start_command_deadline");
        return;
      }
      if (timeoutAtMs - plan.on_start_ms <= startToleranceMs) {
        scheduleDeadlineAt(
          plan.on_start_ms + startToleranceMs + 1,
          expireSlowStart,
          "start_command_deadline",
        );
        return;
      }
      void finishFailed("late", timeoutAtMs - plan.on_start_ms, "start_command_deadline");
    };
    scheduleDeadlineAt(
      plan.on_start_ms + startToleranceMs + 1,
      expireSlowStart,
      "start_command_deadline",
    );
    if (state !== "starting") return;
    let startedAtMs;
    try {
      startedAtMs = await start(session, current);
    } catch {
      clearTimer(deadlineTimer);
      deadlineTimer = null;
      let failureLag = null;
      try {
        const failedAtMs = now();
        if (Number.isSafeInteger(failedAtMs)) failureLag = failedAtMs - plan.on_start_ms;
      } catch {
        // Keep the failure classification closed when the clock read fails.
      }
      await finishFailed("start_error", failureLag, "start_command_failure");
      return;
    }
    clearTimer(deadlineTimer);
    deadlineTimer = null;
    if (state !== "starting") {
      if (session !== null) {
        if (startAttempted) await stopSession();
        await closeSession();
      }
      return;
    }
    const startLagMs = Number.isSafeInteger(startedAtMs)
      ? startedAtMs - plan.on_start_ms : null;
    const afterStartPlan = matchingPlan();
    let afterStartPhase = null;
    try {
      afterStartPhase = afterStartPlan === null || startLagMs === null
        ? null : phaseAt(afterStartPlan, startedAtMs);
    } catch {
      await finishFailed("start_error", startLagMs, "prearm_validation");
      return;
    }
    if (startLagMs === null || startLagMs < 0 || startLagMs > startToleranceMs) {
      await finishFailed("late", startLagMs, "start_command_completion");
      return;
    }
    if (afterStartPlan === null || afterStartPhase !== "on"
        || startedAtMs > afterStartPlan.on_end_ms) {
      await finishFailed("stale", startLagMs, "prearm_validation");
      return;
    }

    state = "started";
    scheduled.delete(key);
    started.add(key);
    try {
      onStarted(session, startedAtMs, controller);
    } catch {
      await finishFailed("start_error", startLagMs, "start_command_completion");
      return;
    }
    report("started", "none", startLagMs, "start_command_completion");
  };

  const prepareAtOffEnd = async () => {
    if (state !== "scheduled") return;
    let actualMs;
    try {
      actualMs = now();
    } catch {
      await finishFailed("start_error", null, "prepare_timer");
      return;
    }
    if (!Number.isSafeInteger(actualMs)) {
      await finishFailed("start_error", null, "prepare_timer");
      return;
    }
    if (actualMs < plan.off_end_ms) {
      scheduleAt(plan.off_end_ms, () => { void prepareAtOffEnd(); }, "prepare_timer");
      return;
    }
    if (actualMs >= plan.on_start_ms) {
      await finishFailed("late", actualMs - plan.on_start_ms, "prepare_timer");
      return;
    }
    const current = matchingPlan();
    if (current === null || actualMs > current.expires_at_ms) {
      await finishFailed("stale", null, "prearm_validation");
      return;
    }
    state = "preparing";
    const expireSlowPreparation = () => {
      if (state !== "preparing") return;
      let timeoutAtMs;
      try {
        timeoutAtMs = now();
      } catch {
        void finishFailed("start_error", null, "prepare_deadline");
        return;
      }
      if (!Number.isSafeInteger(timeoutAtMs)) {
        void finishFailed("start_error", null, "prepare_deadline");
        return;
      }
      if (timeoutAtMs < plan.on_start_ms) {
        scheduleDeadlineAt(plan.on_start_ms, expireSlowPreparation, "prepare_deadline");
        return;
      }
      void finishFailed("late", timeoutAtMs - plan.on_start_ms, "prepare_deadline");
    };
    scheduleDeadlineAt(plan.on_start_ms, expireSlowPreparation, "prepare_deadline");
    if (state !== "preparing") return;
    try {
      const preparedSession = await prepare(current, (candidate) => {
        if (state === "preparing" && session === null) session = candidate;
      });
      if (session === null && preparedSession !== undefined) session = preparedSession;
    } catch {
      clearTimer(deadlineTimer);
      deadlineTimer = null;
      await finishFailed("start_error", null, "prepare_completion");
      return;
    }
    clearTimer(deadlineTimer);
    deadlineTimer = null;
    if (session === null || session === undefined) {
      await finishFailed("start_error", null, "prepare_completion");
      return;
    }
    if (state !== "preparing") {
      await closeSession();
      return;
    }
    const afterPrepareMs = now();
    if (!Number.isSafeInteger(afterPrepareMs) || afterPrepareMs >= plan.on_start_ms) {
      await finishFailed("late", Number.isSafeInteger(afterPrepareMs)
        ? afterPrepareMs - plan.on_start_ms : null, "prepare_completion");
      return;
    }
    const preparedPlan = matchingPlan();
    if (preparedPlan === null || afterPrepareMs > preparedPlan.expires_at_ms) {
      await finishFailed("stale", null, "prearm_validation");
      return;
    }
    state = "prepared";
    scheduleAt(plan.on_start_ms, () => { void startAtEdge(); }, "start_timer");
  };

  const controller = {
    cancel: () => finishFailed("stale", null, "prearm_cancel"),
    complete: async () => {
      if (state !== "started") return;
      state = "complete";
      scheduled.delete(key);
      started.delete(key);
      clearTimers();
      await closeSession();
    },
    get state() { return state; },
  };

  scheduled.add(key);
  scheduleAt(plan.off_end_ms, () => { void prepareAtOffEnd(); }, "prepare_timer");
  if (!scheduled.has(key)) return null;
  return controller;
}
