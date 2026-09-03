import { randomBytes } from "node:crypto";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

const CSP_HEADER = "Content-Security-Policy";
const CSP_REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only";
const CSP_RESPONSE_HEADER = CSP_HEADER;
const NONCE_HEADER = "x-nonce";
const REPORTING_ENDPOINTS = 'csp-endpoint="/api/v1/security/csp-report"';

function contentSecurityPolicy(nonce: string): string {
  return [
    "default-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    `script-src 'self' 'nonce-${nonce}' https://challenges.cloudflare.com https://static.cloudflareinsights.com https://pagead2.googlesyndication.com`,
    "script-src-attr 'none'",
    `style-src 'self' 'nonce-${nonce}'`,
    "style-src-attr 'none'",
    "img-src 'self' blob: https://cdn.old-sparky.com https://steamstore-a.akamaihd.net https://clan.fastly.steamstatic.com https://deadlock.io https://assets-bucket.deadlock-api.com https://i2.ytimg.com https://i3.ytimg.com https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net",
    "connect-src 'self' https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net",
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
