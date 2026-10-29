export const SSR_CAPTURE_SAMPLE_SCHEME: "sha256-runid-requestid-1of8-v1";
export function htmlSsrCaptureDigest(runId: string, requestId: string): string | null;
export function isHtmlSsrCaptureEligible(runId: string, requestId: string): boolean;
