const tokenPattern = /^[A-Za-z0-9._:-]{1,128}$/u;

export function formatWorkspaceApiErrorDiagnostic(input: {
  requestId: string;
  cfRay: string;
  status: number;
  diagnosticRunId?: string | null;
}): string | null {
  if (
    !tokenPattern.test(input.requestId)
    || !tokenPattern.test(input.cfRay)
    || !Number.isInteger(input.status)
    || input.status < 400
    || input.status > 599
  ) {
    return null;
  }
  const diagnosticRunId = input.diagnosticRunId ?? null;
  if (diagnosticRunId !== null && !/^[0-9a-f]{32}$/u.test(diagnosticRunId)) {
    return null;
  }
  const diagnosticField = diagnosticRunId === null ? "" : ` diagnostic_run_id=${diagnosticRunId}`;
  return `ssr_error request_id=${input.requestId} cf_ray=${input.cfRay}${diagnosticField}`
    + ` stage=tournament_workspace family=platform_api_error status=${input.status}`
    + " response_code=unavailable";
}
