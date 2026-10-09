const DIAGNOSTIC_RUN_ID = /^[0-9a-f]{32}$/u;

/** Accept only the exact private run marker issued by the paired plan. */
export function hasAuthorizedDiagnosticRunMarker(value: string | null, runId: string): boolean {
  return value !== null && DIAGNOSTIC_RUN_ID.test(value) && value === runId;
}
