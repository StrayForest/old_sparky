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
    startToleranceMs: number;
  },
): boolean;
