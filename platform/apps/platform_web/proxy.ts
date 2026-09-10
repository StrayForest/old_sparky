import { randomBytes } from "node:crypto";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

const CSP_HEADER = "Content-Security-Policy";
const CSP_REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only";
const CSP_RESPONSE_HEADER = CSP_HEADER;
const NONCE_HEADER = "x-nonce";
const SSR_TRACE_HEADER = "x-platform-ssr-trace";
const SSR_PROXY_START_HEADER = "x-platform-ssr-proxy-start-ms";
const SSR_REQUEST_START_HEADER = "x-platform-ssr-request-start-ms";
const TIMEOUT_DIAGNOSTIC_ID_HEADER = "x-platform-timeout-diagnostic-id";
const TIMEOUT_DIAGNOSTIC_ID_RE = /^tdiag-[0-9]{1,32}-[0-9]{5}$/u;
const REPORTING_ENDPOINTS = 'csp-endpoint="/api/v1/security/csp-report"';

function boundedSampleRate(): number {
  const value = Number(process.env.PLATFORM_SSR_PERF_SAMPLE_RATE);
  return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0.01;
}

function sampleRequest(requestId: string, rate: number): boolean {
  let hash = 2166136261;
  for (const character of requestId) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash / 0x1_0000_0000 < rate;
}

function ssrDiagnosticsEnabled(): boolean {
  return process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
}

function hasTimeoutDiagnosticId(value: string | null): boolean {
  return value !== null && TIMEOUT_DIAGNOSTIC_ID_RE.test(value.trim());
}

function safeEpochMilliseconds(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function contentSecurityPolicy(nonce: string): string {
  return [
    "default-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    `script-src 'nonce-${nonce}' 'unsafe-inline' 'unsafe-eval' 'strict-dynamic' https: http:`,
    "script-src-attr 'none'",
    `style-src 'self' 'nonce-${nonce}'`,
    // AdSense creates inline style attributes on its ad elements. Keep this
    // exception scoped to attributes; nonce-gate <style> elements and external
    // stylesheets above.
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' blob: https://cdn.old-sparky.com https://steamstore-a.akamaihd.net https://clan.fastly.steamstatic.com https://deadlock.io https://assets-bucket.deadlock-api.com https://i2.ytimg.com https://i3.ytimg.com https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net https://csi.gstatic.com",
    "connect-src 'self' https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net https://fundingchoicesmessages.google.com https://csi.gstatic.com",
    "frame-src https://challenges.cloudflare.com https://googleads.g.doubleclick.net https://tpc.googlesyndication.com",
    "font-src 'self'",
    "manifest-src 'self'",
    "media-src 'none'",
    "worker-src 'self'",
    "report-uri /api/v1/security/csp-report",
    "report-to csp-endpoint",
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const requestHeaders = new Headers(request.headers);

  // These values are security state owned by this proxy. Never let a client or
  // an upstream hop choose the nonce/policy consumed by Next.js rendering.
  requestHeaders.delete(CSP_HEADER);
  requestHeaders.delete(CSP_REPORT_ONLY_HEADER);
  requestHeaders.delete(NONCE_HEADER);
  requestHeaders.delete(SSR_TRACE_HEADER);
  requestHeaders.delete(SSR_PROXY_START_HEADER);
  requestHeaders.delete(SSR_REQUEST_START_HEADER);
  if (ssrDiagnosticsEnabled()) {
    const hasDiagnosticId = hasTimeoutDiagnosticId(
      request.headers.get(TIMEOUT_DIAGNOSTIC_ID_HEADER)
    );
    const sampleKey = request.headers.get("x-request-id")
      || request.headers.get("cf-ray")
      || "unknown";
    const requestStartedAtMs = safeEpochMilliseconds(
      request.headers.get(SSR_REQUEST_START_HEADER)
    ) ?? Date.now();
    const proxyStartedAtMs = Date.now();
    requestHeaders.set(
      SSR_TRACE_HEADER,
      hasDiagnosticId || sampleRequest(sampleKey, boundedSampleRate()) ? "1" : "0"
    );
    requestHeaders.set(SSR_REQUEST_START_HEADER, String(requestStartedAtMs));
    requestHeaders.set(SSR_PROXY_START_HEADER, String(proxyStartedAtMs));
  }

  const nonce = randomBytes(16).toString("base64");
  const policy = contentSecurityPolicy(nonce);
  requestHeaders.set(CSP_HEADER, policy);
  requestHeaders.set(NONCE_HEADER, nonce);

  const response = NextResponse.next({
    request: {
      headers: requestHeaders,
    },
  });
  response.headers.delete(CSP_HEADER);
  response.headers.delete(CSP_REPORT_ONLY_HEADER);
  response.headers.set(CSP_RESPONSE_HEADER, policy);
  response.headers.set("Reporting-Endpoints", REPORTING_ENDPOINTS);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api(?:/|$)|_next(?:/|$)|assets(?:/|$)|\\.well-known(?:/|$)|favicon\\.ico$|icon\\.png$|apple-icon\\.png$|manifest\\.webmanifest$|robots\\.txt$|sitemap\\.xml$).*)",
      missing: [
        { type: "header", key: "rsc" },
      ],
    },
  ],
};
