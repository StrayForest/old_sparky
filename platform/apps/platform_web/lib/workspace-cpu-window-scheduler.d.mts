export type WorkspaceCpuWindowPhase = "off" | "on";

export type WorkspaceCpuWindowPlan = {
  readonly run_id: string;
  readonly off_start_ms: number;
  readonly off_end_ms: number;
  readonly on_start_ms: number;
  readonly on_end_ms: number;
  readonly expires_at_ms: number;
};

export type WorkspaceCpuWindowState = {
  readonly started: Set<string>;
  readonly scheduled: Set<string>;
  readonly failed: Set<string>;
};

export type SsrLifecyclePhase = WorkspaceCpuWindowPhase | "none";
export type SsrLifecycleEvent = "mode" | "cpu_window" | "cpu_profile" | "memory";
export type SsrLifecycleReason =
  | "none" | "late" | "stale" | "mismatch" | "start_error"
  | "end_error" | "timing_incomplete" | "profile_bounds" | "profile_stop"
  | "already_claimed";
export type SsrLifecycleStatus =
  | "active" | "trace_only" | "scheduled" | "started" | "skipped"
  | "failed" | "finalized";

export declare function emitSsrWebLifecycleEvent(
  plan: { readonly run_id: string } | null,
  phase: SsrLifecyclePhase,
  event: SsrLifecycleEvent,
  status: SsrLifecycleStatus,
  options?: {
    reason?: SsrLifecycleReason;
    lagMs?: number | null;
    sampleCount?: number | null;
    nowMs?: number;
    writeLine?: (line: string) => void;
  },
): boolean;

export declare function scheduleAuthorizedWorkspaceCpuWindowEdges<
  P extends WorkspaceCpuWindowPlan,
>(
  plan: P,
  freshPlan: P | null,
  authorizedRunId: string | null,
  samePlan: (
    expected: P,
    current: P,
  ) => boolean,
  scheduleWindow: (
    plan: P,
    phase: WorkspaceCpuWindowPhase,
  ) => void,
): boolean;

export declare function claimWorkspaceCpuWindowStart<
  P extends WorkspaceCpuWindowPlan,
>(
  plan: P,
  phase: WorkspaceCpuWindowPhase,
  state: WorkspaceCpuWindowState,
): boolean;

export declare function scheduleWorkspaceCpuWindowStart<
  P extends WorkspaceCpuWindowPlan,
>(
  plan: P,
  phase: WorkspaceCpuWindowPhase,
  options: WorkspaceCpuWindowState & {
    now: () => number;
    schedule: (callback: () => void, delayMs: number) => unknown;
    readPlan: () => P | null;
    samePlan: (
      expected: P,
      current: P | null,
    ) => boolean;
    phaseAt: (
      current: P,
      nowMs: number,
    ) => WorkspaceCpuWindowPhase | null;
    startTarget: (
      current: P,
      phase: WorkspaceCpuWindowPhase,
    ) => number;
    expiresAt: (current: P) => number;
    onStart: (
      current: P,
      phase: WorkspaceCpuWindowPhase,
    ) => void;
    onLifecycle?: (event: {
      readonly status: "scheduled" | "started" | "skipped" | "failed";
      readonly reason: SsrLifecycleReason;
      readonly lagMs: number | null;
    }) => void;
    startToleranceMs: number;
  },
): boolean;
