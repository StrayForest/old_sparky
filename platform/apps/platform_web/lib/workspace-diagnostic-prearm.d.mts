export function authorizedWorkspaceDiagnosticPrearmRunId(
  contextPlan: unknown,
  currentPlan: unknown,
  nowMs: number
): string | null;

export function readAuthorizedWorkspaceDiagnosticPrearmRunId(
  contextPlan: unknown,
  readCurrentPlan: () => unknown,
  nowMs: number
): string | null;

export function withWorkspaceDiagnosticPrearmHeader(
  headers: Record<string, string>,
  runId: string | null
): Record<string, string>;
