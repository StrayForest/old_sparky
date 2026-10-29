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
