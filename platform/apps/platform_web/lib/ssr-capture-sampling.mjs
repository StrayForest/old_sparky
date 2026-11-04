import { createHash } from "node:crypto";

export const SSR_CAPTURE_SAMPLE_SCHEME = "sha256-runid-requestid-1of8-v1";

export function htmlSsrCaptureDigest(runId, requestId) {
  if (!/^[0-9a-f]{32}$/u.test(runId || "")
      || !/^[0-9a-f]{32}$/u.test(requestId || "")) {
    return null;
  }
  return createHash("sha256")
    .update(`${runId}:${requestId}`, "ascii")
    .digest("hex");
}

export function isHtmlSsrCaptureEligible(runId, requestId) {
  const digest = htmlSsrCaptureDigest(runId, requestId);
  return digest !== null && (Number.parseInt(digest.slice(0, 2), 16) & 7) === 0;
}

const HTML_SSR_TRACE_WORKLOADS = new Set([
  "authenticated_html_ssr_capacity_v1",
  "authenticated_html_ssr_capacity_120s_v1",
  "authenticated_html_ssr_cpu_profile_20s_v1",
]);

/**
 * Permit trace correlation during the fixed post-ON drain only for a trusted
 * request whose server-proxy start was inside ON. This does not extend any
 * sampling or CPU-usage window.
 *
 * @param {string} workload
 * @param {number} parentStartMs
 * @param {number} nowMs
 * @param {number} onStartMs
 * @param {number} onEndMs
 * @param {number} expiresAtMs
 */
export function isHtmlSsrTraceDrainEligible(
  workload,
  parentStartMs,
  nowMs,
  onStartMs,
  onEndMs,
  expiresAtMs,
) {
  return HTML_SSR_TRACE_WORKLOADS.has(workload)
    && Number.isSafeInteger(parentStartMs)
    && Number.isSafeInteger(nowMs)
    && Number.isSafeInteger(onStartMs)
    && Number.isSafeInteger(onEndMs)
    && Number.isSafeInteger(expiresAtMs)
    && onStartMs < onEndMs
    && expiresAtMs > onEndMs
    && expiresAtMs - onEndMs <= 15_000
    && parentStartMs >= onStartMs
    && parentStartMs < onEndMs
    && nowMs >= onEndMs
    && nowMs <= expiresAtMs;
}
