export type WorkspaceCpuWindowPhase = "off" | "on";

export type WorkspaceCpuWindowPlan = {
  readonly run_id: string;
  readonly off_start_ms: number;
  readonly on_start_ms: number;
  readonly expires_at_ms: number;
};

export type WorkspaceCpuWindowState = {
  readonly started: Set<string>;
  readonly scheduled: Set<string>;
  readonly failed: Set<string>;
};

export declare function scheduleAuthorizedWorkspaceCpuWindowEdges(
  plan: WorkspaceCpuWindowPlan,
  freshPlan: WorkspaceCpuWindowPlan | null,
  authorizedRunId: string | null,
  samePlan: (
    expected: WorkspaceCpuWindowPlan,
    current: WorkspaceCpuWindowPlan,
  ) => boolean,
  scheduleWindow: (
    plan: WorkspaceCpuWindowPlan,
    phase: WorkspaceCpuWindowPhase,
  ) => void,
): boolean;

export declare function claimWorkspaceCpuWindowStart(
  plan: WorkspaceCpuWindowPlan,
  phase: WorkspaceCpuWindowPhase,
  state: WorkspaceCpuWindowState,
): boolean;

export declare function scheduleWorkspaceCpuWindowStart(
  plan: WorkspaceCpuWindowPlan,
  phase: WorkspaceCpuWindowPhase,
  options: WorkspaceCpuWindowState & {
    now: () => number;
    schedule: (callback: () => void, delayMs: number) => unknown;
    readPlan: () => WorkspaceCpuWindowPlan | null;
    samePlan: (
      expected: WorkspaceCpuWindowPlan,
      current: WorkspaceCpuWindowPlan | null,
    ) => boolean;
    phaseAt: (
      current: WorkspaceCpuWindowPlan,
      nowMs: number,
    ) => WorkspaceCpuWindowPhase | null;
    startTarget: (
      current: WorkspaceCpuWindowPlan,
      phase: WorkspaceCpuWindowPhase,
    ) => number;
    expiresAt: (current: WorkspaceCpuWindowPlan) => number;
    onStart: (
      current: WorkspaceCpuWindowPlan,
      phase: WorkspaceCpuWindowPhase,
    ) => void;
    startToleranceMs: number;
  },
): boolean;
