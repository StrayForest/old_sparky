const PHASES = new Set(["off", "on"]);

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
  if (started.has(key) || scheduled.has(key) || failed.has(key)) return false;

  let targetMs;
  let nowMs;
  try {
    targetMs = startTarget(plan, phase);
    nowMs = now();
  } catch {
    failed.add(key);
    return false;
  }
  if (!Number.isSafeInteger(targetMs) || !Number.isSafeInteger(nowMs)
      || targetMs <= nowMs) {
    failed.add(key);
    return false;
  }

  scheduled.add(key);
  const failClosed = () => {
    scheduled.delete(key);
    failed.add(key);
  };

  const scheduleAtEdge = (delayMs) => {
    try {
      schedule(startAtEdge, Math.max(0, delayMs));
    } catch {
      failClosed();
    }
  };

  const startAtEdge = () => {
    let actualMs;
    let current;
    try {
      actualMs = now();
      current = readPlan();
    } catch {
      failClosed();
      return;
    }
    if (!Number.isSafeInteger(actualMs) || !samePlan(plan, current)) {
      failClosed();
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
      failClosed();
      return;
    }
    if (!Number.isSafeInteger(currentTarget) || currentTarget !== targetMs
        || !Number.isSafeInteger(currentExpiry) || actualMs > currentExpiry
        || actualMs - targetMs > startToleranceMs || currentPhase !== phase) {
      failClosed();
      return;
    }
    scheduled.delete(key);
    try {
      onStart(current, phase);
    } catch {
      failed.add(key);
    }
  };

  scheduleAtEdge(targetMs - nowMs);
  return scheduled.has(key);
}
