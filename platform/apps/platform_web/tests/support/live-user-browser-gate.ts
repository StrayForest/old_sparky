import type { BrowserContext } from "@playwright/test";

export const LIVE_USER_BROWSER_GATE_ATTACHMENT_NAME = "live-user-browser-gate-v2";

const CSP_CONSOLE_PREFIX = "__platform_liveqa_csp_violation__:";
const REPORT_ONLY_FRAME_ANCESTORS_WARNING =
  "The Content Security Policy directive 'frame-ancestors' is ignored when delivered in a report-only policy.";
const MAX_EVENTS = 32;
const MAX_COUNT = 255;

export type LiveUserBrowserGateKind =
  | "csp_violation"
  | "csp_console"
  | "forbidden_request"
  | "page_error"
  | "request_failed";
export type LiveUserBrowserGatePhase = "journey" | "logout";

type LiveUserBrowserGateCounts = Record<LiveUserBrowserGateKind, number>;
type LiveUserBrowserGateEvent = {
  kind: LiveUserBrowserGateKind;
  phase: LiveUserBrowserGatePhase;
  disposition?: "enforce" | "report_only" | "unknown";
  directive?: string;
  resource_type?: string;
  method?: string;
  route?: string;
  failure_class?: string;
};

export type LiveUserBrowserGateCapture = {
  phase: LiveUserBrowserGatePhase;
  failures: string[];
  counts: LiveUserBrowserGateCounts;
  counts_truncated: boolean;
  events_truncated: boolean;
  events: LiveUserBrowserGateEvent[];
};

export function createLiveUserBrowserGateCapture(): LiveUserBrowserGateCapture {
  return {
    phase: "journey",
    failures: [],
    counts: {
      csp_violation: 0,
      csp_console: 0,
      forbidden_request: 0,
      page_error: 0,
      request_failed: 0,
    },
    counts_truncated: false,
    events_truncated: false,
    events: [],
  };
}

function record(
  capture: LiveUserBrowserGateCapture,
  kind: LiveUserBrowserGateKind,
  details: Omit<LiveUserBrowserGateEvent, "kind" | "phase"> = {},
) {
  if (capture.counts[kind] < MAX_COUNT) capture.counts[kind] += 1;
  else capture.counts_truncated = true;
  if (capture.events.length >= MAX_EVENTS) {
    capture.events_truncated = true;
    return;
  }
  capture.events.push({ kind, phase: capture.phase, ...details });
}

function safeDisposition(value: string): "enforce" | "report_only" | "unknown" {
  if (value === "enforce") return "enforce";
  if (value === "report") return "report_only";
  return "unknown";
}

function safeDirective(value: string): string {
  const directive = value.toLowerCase();
  const known = new Set([
    "base-uri", "connect-src", "default-src", "font-src", "form-action",
    "frame-ancestors", "frame-src", "img-src", "media-src", "object-src",
    "script-src", "style-src", "upgrade-insecure-requests",
  ]);
  return known.has(directive) ? directive : "other";
}

function safeMethod(value: string): string {
  return new Set(["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]).has(value)
    ? value
    : "OTHER";
}

function safeResourceType(value: string): string {
  return new Set([
    "document", "image", "stylesheet", "script", "font", "media", "xhr", "fetch",
  ]).has(value) ? value : "other";
}

function safeRoute(url: URL, expectedOrigin: string): string {
  if (url.origin !== expectedOrigin) return "external";
  if (url.pathname === "/") return "home";
  if (/^\/api\/v1\/sessions(?:\/|$)/u.test(url.pathname)) return "api_session";
  if (/^\/api\/v1\/profiles(?:\/|$)/u.test(url.pathname)) return "api_profile";
  if (/^\/api\/v1\/tournaments(?:\/|$)/u.test(url.pathname)) return "api_tournament";
  if (/^\/(?:profile|account)(?:\/|$)/u.test(url.pathname)) return "account";
  if (/^\/tournaments(?:\/|$)/u.test(url.pathname)) return "tournament";
  if (/^(?:\/_next\/|\/assets\/)/u.test(url.pathname)) return "static";
  return "other";
}

function safeFailureClass(value: string): string {
  if (/ERR_NAME_NOT_RESOLVED|ERR_DNS/u.test(value)) return "dns";
  if (/ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT/u.test(value)) return "timeout";
  if (/ERR_CERT|ERR_SSL|ERR_TLS/u.test(value)) return "tls";
  if (/ERR_CONNECTION|ERR_ADDRESS_UNREACHABLE/u.test(value)) return "connection";
  if (/ERR_ABORTED/u.test(value)) return "aborted";
  if (value === "Load request cancelled") return "cancelled";
  return "other";
}

export async function installLiveUserBrowserGate(
  context: BrowserContext,
  label: string,
  expectedOrigin: string,
  capture: LiveUserBrowserGateCapture,
  confirmedMutations: Set<string>,
) {
  await context.addInitScript((prefix) => {
    if (window.top !== window) return;
    document.addEventListener("securitypolicyviolation", (event) => {
      console.warn(
        `${prefix}${event.disposition.slice(0, 16)}:${event.effectiveDirective.slice(0, 64)}`,
      );
    });
  }, CSP_CONSOLE_PREFIX);

  context.on("request", (request) => {
    let parsed: URL;
    try {
      parsed = new URL(request.url());
    } catch {
      return;
    }
    if (
      parsed.hostname === "challenges.cloudflare.com"
      || (
        parsed.origin === expectedOrigin
        && ["/auth/login", "/auth/register", "/reset-password", "/verify-email"].includes(
          parsed.pathname,
        )
      )
    ) {
      record(capture, "forbidden_request", {
        resource_type: safeResourceType(request.resourceType()),
        method: safeMethod(request.method()),
        route: safeRoute(parsed, expectedOrigin),
      });
      capture.failures.push(`${label}:forbidden-auth-automation`);
    }
  });

  context.on("page", (page) => {
    page.on("console", (message) => {
      const text = message.text();
      if (text.startsWith(CSP_CONSOLE_PREFIX)) {
        const [disposition = "", directive = ""] = text
          .slice(CSP_CONSOLE_PREFIX.length)
          .split(":", 2);
        record(capture, "csp_violation", {
          disposition: safeDisposition(disposition),
          directive: safeDirective(directive),
        });
        capture.failures.push(`${label}:csp_violation`);
        return;
      }
      if (
        message.type() === "error"
        && /content security policy|refused to (?:execute|apply|load|connect)/iu.test(text)
        && text !== REPORT_ONLY_FRAME_ANCESTORS_WARNING
      ) {
        record(capture, "csp_console");
        capture.failures.push(`${label}:csp_console`);
      }
    });
    page.on("pageerror", () => {
      record(capture, "page_error");
      capture.failures.push(`${label}:page_error`);
    });
    page.on("requestfailed", (request) => {
      const errorText = request.failure()?.errorText ?? "failed";
      let route = "unknown";
      try {
        const parsed = new URL(request.url());
        route = safeRoute(parsed, expectedOrigin);
        const headers = request.headers();
        const isExpectedNextPrefetchAbort = (
          errorText === "net::ERR_ABORTED"
          && request.method() === "GET"
          && parsed.origin === expectedOrigin
          && (
            parsed.searchParams.has("_rsc")
            || headers.rsc === "1"
            || headers["next-router-prefetch"] === "1"
            || headers["next-router-segment-prefetch"] === "1"
          )
        );
        const isExpectedNavigationApiCancellation = (
          errorText === "Load request cancelled"
          && request.method() === "GET"
          && parsed.origin === expectedOrigin
          && ["/api/v1/content/support/status", "/api/v1/users/me"].includes(parsed.pathname)
        );
        const isConfirmedMutationTeardown = (
          errorText === "net::ERR_ABORTED"
          && parsed.origin === expectedOrigin
          && confirmedMutations.has(`${request.method()}:${parsed.pathname}`)
        );
        if (
          isExpectedNextPrefetchAbort
          || isExpectedNavigationApiCancellation
          || isConfirmedMutationTeardown
        ) return;
      } catch {
        route = "unknown";
      }
      record(capture, "request_failed", {
        resource_type: safeResourceType(request.resourceType()),
        method: safeMethod(request.method()),
        route,
        failure_class: safeFailureClass(errorText),
      });
      capture.failures.push(`${label}:request_failed`);
    });
  });
}

export function liveUserBrowserGateAttachment(capture: LiveUserBrowserGateCapture) {
  return {
    schema: 2,
    coverage: "journey_logout_before_context_close",
    counts: capture.counts,
    counts_truncated: capture.counts_truncated,
    events_truncated: capture.events_truncated,
    events: capture.events,
  };
}
