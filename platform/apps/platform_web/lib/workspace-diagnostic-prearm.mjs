const SSR_TRACE_HEADER = "x-platform-ssr-trace";
const RUN_ID_RE = /^[0-9a-f]{32}$/u;
const HTML_WORKLOAD = "authenticated_html_ssr_capacity_v1";

export function authorizedWorkspaceDiagnosticPrearmRunId(contextPlan, currentPlan, nowMs) {
  if (contextPlan === null || typeof contextPlan !== "object"
      || currentPlan === null || typeof currentPlan !== "object"
      || !Number.isSafeInteger(nowMs)
      || JSON.stringify(contextPlan) !== JSON.stringify(currentPlan)) return null;
  const plan = contextPlan;
  const leadMs = plan.off_start_ms - nowMs;
  if (plan.workload !== HTML_WORKLOAD || !RUN_ID_RE.test(plan.run_id)
      || !(leadMs > 0 && leadMs <= 52_000)) return null;
  return plan.run_id;
}

export function readAuthorizedWorkspaceDiagnosticPrearmRunId(
  contextPlan, readCurrentPlan, nowMs,
) {
  if (contextPlan === null || typeof contextPlan !== "object"
      || typeof readCurrentPlan !== "function") return null;
  return authorizedWorkspaceDiagnosticPrearmRunId(
    contextPlan, readCurrentPlan(), nowMs,
  );
}

export function withWorkspaceDiagnosticPrearmHeader(headers, runId) {
  const result = { ...headers };
  if (typeof runId === "string" && RUN_ID_RE.test(runId)) {
    result[SSR_TRACE_HEADER] = runId;
  }
  return result;
}
