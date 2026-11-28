import { expect, test } from "@playwright/test";
import type { Locator } from "@playwright/test";
import LiveCountReporter from "../support/live-count-reporter.cjs";
import {
  browserNavigationDocument,
  browserNavigationPath,
  classifyBrowserRequestFailure,
  collectLiveImageInventory,
  finalizeBrowserFailureEvidence,
  isCausallyBoundNavigationTeardownCancellation,
  isPotentialNavigationTeardownCancellation,
  type BrowserNavigationCancellation,
  type BrowserNavigationIntent,
  type BrowserFailureAttachmentState,
  type BrowserRequestFailureEventV2,
  type BrowserRequestFailureEvidence,
} from "../support/live-image-inventory";
import { takeObjectiveScreenshotWithDiagnostics } from "../support/objective-screenshot-diagnostic";
import { assertLiveQaChromiumSandbox } from "../support/live-qa-sandbox";
import { expectRenderedPatchChangeList, patchAcceptanceIssues } from "../support/patch-acceptance";
import type { PlatformPatchDetail, PlatformPatchSection } from "../../lib/platform-types";

type BrowserRequestFailureStart = {
  startedAt: number;
  routeScope: BrowserRequestFailureEvidence["events"][number]["route_scope"];
  sourceDocument: string | null;
  sourcePath: string | null;
  sourceNavigationEpoch: number;
  mainFrame: boolean;
};

type BrowserEvidence = {
  consoleCspErrors: string[];
  cspViolations: Array<{
    blockedURI: string;
    disposition: string;
    effectiveDirective: string;
    documentURI: string;
    sourceFile: string;
    lineNumber: number;
    columnNumber: number;
  }>;
  pageErrors: string[];
  pageErrorObjects: Error[];
  pageErrorsTruncated: boolean;
  pageErrorCollectorReady: boolean;
  nextScriptResponses: Array<{ url: string; status: number; resourceType: string }>;
  nextScriptPaths: Set<string>;
  unexpectedRequestFailureCount: number;
  requestFailureStarts: WeakMap<import("@playwright/test").Request, BrowserRequestFailureStart>;
  mainFrameNavigationEpoch: number;
  nextNavigationIntentToken: number;
  pendingNavigationCancellations: Array<{
    request: import("@playwright/test").Request;
    failureClass: BrowserRequestFailureEvidence["events"][number]["failure_class"];
    failureRouteScope: BrowserRequestFailureEventV2["failure_route_scope"];
    failureAt: number;
    failureNavigationEpoch: number;
    cancellation: BrowserNavigationCancellation;
    start: BrowserRequestFailureStart;
    intent: BrowserNavigationIntent;
  }>;
  navigationIntent: BrowserNavigationIntent | null;
  requestFailureEvents: BrowserRequestFailureEventV2[];
  requestFailureEventsTruncated: boolean;
  imageInventoryRoutes: unknown[];
  attachedImageInventoryRoutes: Set<string>;
  requestFailureEvidenceAttached: boolean;
  imageBodyFailureObserved: boolean;
  unexpectedAuthAutomationRequests: string[];
};

const browserEvidence = new WeakMap<import("@playwright/test").Page, BrowserEvidence>();
const reportOnlyFrameAncestorsWarning =
  "The Content Security Policy directive 'frame-ancestors' is ignored when delivered in a report-only policy.";
const googleOwnedInlineStyleSelector = [
  "ins.adsbygoogle",
  "ins.adsbygoogle [id^=\"aswift_\"][id$=\"_host\"]",
  "iframe[name=\"googlefcPresent\"]",
  "iframe[name=\"googlefcInactive\"]",
  "iframe[name=\"googlefcLoaded\"]",
  "iframe[name=\"__tcfapiLocator\"]",
].join(", ");
const expectedWebKitPrefetchCancellationPaths = new Set([
  "/privacy",
  "/terms",
  "/profile/me",
]);
const MAX_BROWSER_REQUEST_FAILURE_EVENTS = 32;
const browserRequestMethods = new Set(["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]);

function browserRequestRouteScope(
  value: string,
): BrowserRequestFailureEvidence["events"][number]["route_scope"] {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "unknown";
    const pathname = parsed.pathname;
    if (pathname === "/") return "home";
    if (/^\/patches\/[^/]+(?:\/|$)/u.test(pathname)) return "patch";
    if (pathname === "/tournaments" || pathname.startsWith("/tournaments/")) {
      return "tournaments";
    }
    return "other";
  } catch {
    return "unknown";
  }
}

function browserRequestResourceType(
  value: string,
): BrowserRequestFailureEvidence["events"][number]["resource_type"] {
  if (["document", "image", "stylesheet", "script", "font", "media", "xhr", "fetch"].includes(value)) {
    return value as BrowserRequestFailureEvidence["events"][number]["resource_type"];
  }
  return value ? "other" : "unknown";
}

function browserRequestMethod(value: string): BrowserRequestFailureEventV2["method"] {
  return browserRequestMethods.has(value)
    ? value as BrowserRequestFailureEventV2["method"]
    : "OTHER";
}

function browserRequestEndpointFamily(
  requestUrl: string,
  baseUrl: string,
): BrowserRequestFailureEventV2["endpoint_family"] {
  try {
    const parsed = new URL(requestUrl);
    const base = new URL(baseUrl);
    if (parsed.origin !== base.origin) return "external";
    const pathname = parsed.pathname;
    if (/^\/api\/v1\/content\/patches\/[^/]+(?:\/|$)/u.test(pathname)) {
      return "api_patch_content";
    }
    if (pathname === "/api/v1/content" || pathname.startsWith("/api/v1/content/")) {
      return "api_content";
    }
    if (pathname === "/api/v1/users" || pathname.startsWith("/api/v1/users/")) {
      return "api_users";
    }
    if (pathname === "/api/v1/tournaments" || pathname.startsWith("/api/v1/tournaments/")) {
      return "api_tournaments";
    }
    if (pathname === "/api" || pathname.startsWith("/api/")) return "api_other";
    return "same_origin_other";
  } catch {
    return "unknown";
  }
}

function requestPath(value: string): string | null {
  return browserNavigationPath(value);
}

function browserRequestSameOrigin(requestUrl: string, baseUrl: string): boolean {
  try {
    return new URL(requestUrl).origin === new URL(baseUrl).origin;
  } catch {
    return false;
  }
}

function browserRequestNavigationPhase(
  start: BrowserRequestFailureStart | undefined,
  intent: BrowserNavigationIntent | null,
): BrowserRequestFailureEventV2["navigation_phase"] {
  if (!start) return "unknown";
  if (!intent) return "none";
  return intent.sourcePath === intent.targetPath ? "pending_same_route" : "pending_route_change";
}

function recordUnexpectedBrowserRequestFailure(
  evidence: BrowserEvidence,
  request: import("@playwright/test").Request,
  failureClass: BrowserRequestFailureEvidence["events"][number]["failure_class"],
  start: BrowserRequestFailureStart | undefined,
  intent: BrowserNavigationIntent | null,
  failureRouteScope: BrowserRequestFailureEventV2["failure_route_scope"],
  failureAt: number,
  failureNavigationEpoch: number,
): void {
  const baseUrl = process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1";
  evidence.unexpectedRequestFailureCount += 1;
  if (evidence.requestFailureEvents.length >= MAX_BROWSER_REQUEST_FAILURE_EVENTS) {
    evidence.requestFailureEventsTruncated = true;
    return;
  }
  evidence.requestFailureEvents.push({
    ordinal: evidence.requestFailureEvents.length + 1,
    method: browserRequestMethod(request.method()),
    resource_type: browserRequestResourceType(request.resourceType()),
    route_scope: start?.routeScope ?? "unknown",
    failure_route_scope: failureRouteScope,
    endpoint_family: browserRequestEndpointFamily(request.url(), baseUrl),
    navigation_relation: start
      ? start.sourceNavigationEpoch === failureNavigationEpoch ? "same" : "changed"
      : "unknown",
    navigation_phase: browserRequestNavigationPhase(start, intent),
    failure_class: failureClass,
    duration_bucket: browserRequestDurationBucket(
      start ? failureAt - start.startedAt : undefined,
    ),
  });
}

async function liveQaGoto(
  page: import("@playwright/test").Page,
  route: string,
): Promise<import("@playwright/test").Response | null> {
  const evidence = browserEvidence.get(page);
  if (!evidence) return page.goto(route);
  if (evidence.navigationIntent) {
    throw new Error("a controlled live QA navigation is already active");
  }
  const baseUrl = process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1";
  const currentUrl = page.url();
  const intentBase = currentUrl.startsWith("http://") || currentUrl.startsWith("https://")
    ? currentUrl
    : baseUrl;
  const targetUrl = new URL(route, intentBase);
  const targetDocument = browserNavigationDocument(targetUrl.toString());
  const targetPath = requestPath(targetUrl.toString());
  if (!targetDocument || !targetPath) {
    throw new Error("controlled live QA navigation target is not an HTTP document");
  }
  const intent: BrowserNavigationIntent = {
    token: ++evidence.nextNavigationIntentToken,
    startedAt: performance.now(),
    sourceDocument: browserNavigationDocument(currentUrl),
    sourcePath: requestPath(currentUrl) ?? "",
    sourceNavigationEpoch: evidence.mainFrameNavigationEpoch,
    targetDocument,
    targetPath,
    committed: false,
    responseStatus: null,
    finalDocument: null,
    finalPath: null,
  };
  // Record the explicit navigation intent synchronously before Playwright starts
  // the destination request. Only cancellations from this source document can
  // remain provisional while this exact target navigation is in flight.
  evidence.navigationIntent = intent;
  try {
    const response = await page.goto(route);
    const status = response?.status() ?? null;
    const finalDocument = browserNavigationDocument(page.url());
    const finalPath = requestPath(page.url());
    intent.responseStatus = status;
    intent.finalDocument = finalDocument;
    intent.finalPath = finalPath;
    intent.committed = status !== null
      && status >= 200
      && status < 300
      && finalDocument === intent.targetDocument
      && finalPath === intent.targetPath;
    for (const pending of evidence.pendingNavigationCancellations.splice(0)) {
      if (!isCausallyBoundNavigationTeardownCancellation(pending.cancellation, intent)) {
        recordUnexpectedBrowserRequestFailure(
          evidence,
          pending.request,
          pending.failureClass,
          pending.start,
          pending.intent,
          pending.failureRouteScope,
          pending.failureAt,
          pending.failureNavigationEpoch,
        );
      }
    }
    evidence.navigationIntent = null;
    return response;
  } catch (error) {
    for (const pending of evidence.pendingNavigationCancellations.splice(0)) {
      recordUnexpectedBrowserRequestFailure(
        evidence,
        pending.request,
        pending.failureClass,
        pending.start,
        pending.intent,
        pending.failureRouteScope,
        pending.failureAt,
        pending.failureNavigationEpoch,
      );
    }
    evidence.navigationIntent = null;
    throw error;
  }
}

function browserRequestDurationBucket(
  durationMs: number | undefined,
): BrowserRequestFailureEvidence["events"][number]["duration_bucket"] {
  if (durationMs === undefined || !Number.isFinite(durationMs) || durationMs < 0) {
    return "unavailable";
  }
  if (durationMs < 100) return "<100ms";
  if (durationMs < 1_000) return "<1s";
  if (durationMs < 5_000) return "<5s";
  return ">=5s";
}
async function attachPageErrorEvidence(
  page: import("@playwright/test").Page,
  testInfo: import("@playwright/test").TestInfo,
  evidence: BrowserEvidence,
): Promise<void> {
  if (!evidence.pageErrorCollectorReady) return;
  try {
    const attachment = LiveCountReporter.createPageErrorAttachment({
      pageUrl: page.url(),
      errors: evidence.pageErrorObjects,
      successfulScriptResponses: evidence.nextScriptResponses,
      truncated: evidence.pageErrorsTruncated,
    });
    if (!attachment) return;
    await testInfo.attach(attachment.name, {
      body: attachment.body,
      contentType: attachment.contentType,
    });
  } catch {
    // Diagnostic attachment failure must not change the browser QA result.
  }
}

async function currentIndexedPatchId(
  request: import("@playwright/test").APIRequestContext,
): Promise<string> {
  const response = await request.get("/api/v1/content/patch-index");
  expect(response.ok()).toBe(true);
  const payload: unknown = await response.json();
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new Error("Live patch index response is not an object.");
  }
  const patches = (payload as Record<string, unknown>).patches;
  if (!Array.isArray(patches) || patches.length === 0) {
    throw new Error("Live patch index has no current patch.");
  }
  const firstPatch = patches[0];
  if (typeof firstPatch !== "object" || firstPatch === null || Array.isArray(firstPatch)) {
    throw new Error("Live patch index entry is not an object.");
  }
  const patchId = (firstPatch as Record<string, unknown>).id;
  if (typeof patchId !== "string" || !/^\d{1,32}$/u.test(patchId)) {
    throw new Error("Live patch index entry has an invalid id.");
  }
  return patchId;
}

async function currentIndexedPatchIds(
  request: import("@playwright/test").APIRequestContext,
): Promise<string[]> {
  const response = await request.get("/api/v1/content/patch-index");
  expect(response.ok()).toBe(true);
  const payload: unknown = await response.json();
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new Error("Live patch index response is not an object.");
  }
  const patches = (payload as Record<string, unknown>).patches;
  if (!Array.isArray(patches)) {
    throw new Error("Live patch index has no patch list.");
  }
  const ids = patches.slice(0, 4).map((entry) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      throw new Error("Live patch index entry is not an object.");
    }
    const id = (entry as Record<string, unknown>).id;
    if (typeof id !== "string" || !/^\d{1,32}$/u.test(id)) {
      throw new Error("Live patch index entry has an invalid id.");
    }
    return id;
  });
  return ids;
}

function sectionHeading(section: PlatformPatchSection): string {
  if (section.objective_key === "urn") return "Урна";
  if (section.objective_key === "unstable_rift") return "Нестабильный разлом";
  if (section.objective_key === "mid_boss") return "Mid Boss";
  const displayName = section.hero_display_name?.trim();
  return section.kind === "hero"
    && displayName
    && displayName.toLocaleLowerCase() !== section.title.trim().toLocaleLowerCase()
    ? displayName
    : section.title;
}

type DecodedImageFailureContext = {
  patchId: string;
  role: "hero" | "objective" | "ability";
  sectionIndex: number;
  elementIndex: number | null;
};

type DecodedImageSample = {
  state: "decoded" | "not_complete" | "decode_pending" | "decode_rejected" | "zero_dimensions";
  decodeError: "none" | "timeout" | "rejected" | "other";
  currentSrcPresent: boolean;
  currentSrcChanged: boolean;
  width: number;
  height: number;
};

function imageDimensionBucket(value: number): "zero" | "1-32" | "33-128" | "129-512" | ">512" | "unavailable" {
  if (!Number.isSafeInteger(value) || value < 0) return "unavailable";
  if (value === 0) return "zero";
  if (value <= 32) return "1-32";
  if (value <= 128) return "33-128";
  if (value <= 512) return "129-512";
  return ">512";
}

async function expectDecodedImage(
  image: Locator,
  context: DecodedImageFailureContext,
  testInfo: import("@playwright/test").TestInfo,
): Promise<void> {
  await image.scrollIntoViewIfNeeded();
  const latest: { sample: DecodedImageSample | null } = { sample: null };
  const trackerKey = `__oldSparkyDecodedImageV1_${context.patchId}_${context.role}_${context.sectionIndex}_${context.elementIndex ?? "none"}`;
  try {
    await expect.poll(
      async () => {
        const sample = await image.evaluate((element, key): DecodedImageSample => {
          const loadedImage = element as HTMLImageElement;
          const currentSrc = loadedImage.currentSrc;
          const state = window as unknown as Record<string, unknown>;
          type DecodeTracker = DecodedImageSample & { initialCurrentSrc: string | null };
          const existing = state[key] as DecodeTracker | undefined;
          if (existing) {
            if (!existing.initialCurrentSrc && currentSrc) existing.initialCurrentSrc = currentSrc;
            existing.currentSrcChanged = Boolean(
              existing.initialCurrentSrc && currentSrc
              && existing.initialCurrentSrc !== currentSrc,
            );
            existing.currentSrcPresent = Boolean(currentSrc);
            existing.width = loadedImage.naturalWidth;
            existing.height = loadedImage.naturalHeight;
            const sample = { ...existing };
            delete (sample as Partial<DecodeTracker>).initialCurrentSrc;
            if (existing.state === "decoded") delete state[key];
            return sample;
          }
          if (!loadedImage.complete) {
            return {
              state: "not_complete",
              decodeError: "none",
              currentSrcPresent: Boolean(currentSrc),
              currentSrcChanged: false,
              width: loadedImage.naturalWidth,
              height: loadedImage.naturalHeight,
            };
          }
          const tracker: DecodeTracker = {
            state: "decode_pending",
            decodeError: "none",
            currentSrcPresent: Boolean(currentSrc),
            currentSrcChanged: false,
            width: loadedImage.naturalWidth,
            height: loadedImage.naturalHeight,
            initialCurrentSrc: currentSrc || null,
          };
          state[key] = tracker;
          void Promise.resolve().then(() => loadedImage.decode()).then(() => {
            tracker.width = loadedImage.naturalWidth;
            tracker.height = loadedImage.naturalHeight;
            tracker.state = tracker.width > 0 && tracker.height > 0 ? "decoded" : "zero_dimensions";
          }).catch((error: unknown) => {
            const name = error instanceof Error ? error.name : "";
            tracker.state = "decode_rejected";
            tracker.decodeError = name === "TimeoutError"
              ? "timeout"
              : name === "EncodingError" || name === "InvalidStateError"
                ? "rejected" : "other";
          });
          const sample = { ...tracker };
          delete (sample as Partial<DecodeTracker>).initialCurrentSrc;
          return sample;
        }, trackerKey);
        latest.sample = sample;
        return sample.state;
      },
      { message: "patch image should decode", timeout: 15_000 },
    ).toBe("decoded");
  } catch (error) {
    try {
      const contextIsClosed = /^[1-9][0-9]{0,15}$/u.test(context.patchId)
        && Number.isSafeInteger(context.sectionIndex)
        && context.sectionIndex >= 0 && context.sectionIndex <= 127
        && (context.role === "ability"
          ? context.elementIndex !== null
            && Number.isSafeInteger(context.elementIndex)
            && context.elementIndex >= 0 && context.elementIndex <= 1023
          : context.elementIndex === null);
      if (contextIsClosed) {
        const sample = latest.sample;
        await testInfo.attach("decoded-image-failure-v1", {
          body: Buffer.from(JSON.stringify({
            schema: 1,
            patch_id: context.patchId,
            role: context.role,
            section_index: context.sectionIndex,
            element_index: context.elementIndex,
            state: sample?.state === "decode_rejected" || sample?.state === "zero_dimensions"
              ? sample.state : sample?.state === "decode_pending" ? "decode_pending" : "not_complete",
            decode_error: sample?.decodeError ?? "other",
            current_src_present: sample?.currentSrcPresent ?? false,
            current_src_changed: sample?.currentSrcChanged ?? false,
            width_bucket: sample ? imageDimensionBucket(sample.width) : "unavailable",
            height_bucket: sample ? imageDimensionBucket(sample.height) : "unavailable",
          }), "ascii"),
          contentType: "application/json",
        });
      }
    } catch {
      // Best-effort diagnostic must never replace the image assertion failure.
    }
    throw error;
  }
}

function hasOwnerPrefix(change: string): boolean {
  return /^(?:Nurse|Медсестра|Mid Boss|Средний босс|Items|Heroes|Предметы|Герои)\s*:/iu.test(change.trim());
}

function hasRawPatchMarkup(change: string): boolean {
  return /STEAM_CLAN_LOC_IMAGE|clan\.fastly\.steamstatic\.com|\[\s*\/?\s*img\b|\[\s*url\b/iu.test(change);
}

function expectCleanPatchChanges(changes: string[]): void {
  for (const change of changes) {
    expect(hasOwnerPrefix(change)).toBe(false);
    expect(hasRawPatchMarkup(change)).toBe(false);
  }
}

test.beforeEach(async ({ page, browserName }) => {
  const evidence: BrowserEvidence = {
    consoleCspErrors: [],
    cspViolations: [],
    pageErrors: [],
    pageErrorObjects: [],
    pageErrorsTruncated: false,
    pageErrorCollectorReady: false,
    nextScriptResponses: [],
    nextScriptPaths: new Set(),
    unexpectedRequestFailureCount: 0,
    requestFailureStarts: new WeakMap(),
    mainFrameNavigationEpoch: 0,
    nextNavigationIntentToken: 0,
    pendingNavigationCancellations: [],
    navigationIntent: null,
    requestFailureEvents: [],
    requestFailureEventsTruncated: false,
    imageInventoryRoutes: [],
    attachedImageInventoryRoutes: new Set(),
    requestFailureEvidenceAttached: false,
    imageBodyFailureObserved: false,
    unexpectedAuthAutomationRequests: [],
  };
  browserEvidence.set(page, evidence);
  let mainFrameNavigationEpoch = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) {
      mainFrameNavigationEpoch += 1;
      evidence.mainFrameNavigationEpoch = mainFrameNavigationEpoch;
    }
  });
  page.on("request", (request) => {
    let routeScope: BrowserRequestFailureEvidence["events"][number]["route_scope"] = "unknown";
    let frameUrl: string | null = null;
    let mainFrame = false;
    try {
      const frame = request.frame();
      frameUrl = frame.url();
      routeScope = browserRequestRouteScope(frameUrl);
      mainFrame = frame === page.mainFrame();
    } catch {
      // Requests without an attached frame keep an explicit unknown scope.
    }
    const startedAt = performance.now();
    evidence.requestFailureStarts.set(request, {
      startedAt,
      routeScope,
      sourceDocument: frameUrl ? browserNavigationDocument(frameUrl) : null,
      sourcePath: frameUrl ? requestPath(frameUrl) : null,
      sourceNavigationEpoch: mainFrameNavigationEpoch,
      mainFrame,
    });
    const parsed = new URL(request.url());
    if (
      parsed.hostname === "challenges.cloudflare.com"
      || ["/auth/login", "/auth/register", "/reset-password"].includes(
        parsed.pathname,
      )
    ) {
      evidence.unexpectedAuthAutomationRequests.push(
        `${request.method()} ${parsed.origin}${parsed.pathname}`,
      );
    }
  });
  page.on("console", (message) => {
    if (
      message.type() === "error"
      && /content security policy|refused to (?:execute|apply|load|connect)/iu.test(message.text())
    ) {
      if (message.text() !== reportOnlyFrameAncestorsWarning) {
        evidence.consoleCspErrors.push(summarizeCspConsoleError(message.text()));
      }
    }
  });
  page.on("response", (response) => {
    const scriptResponse = {
      url: response.url(),
      status: response.status(),
      resourceType: response.request().resourceType(),
    };
    const scriptPath = LiveCountReporter.trustedSuccessfulNextScriptPath(scriptResponse);
    if (
      !scriptPath
      || evidence.nextScriptPaths.has(scriptPath)
      || evidence.nextScriptResponses.length >= LiveCountReporter.MAX_TRACKED_NEXT_SCRIPTS
    ) return;
    evidence.nextScriptPaths.add(scriptPath);
    evidence.nextScriptResponses.push(scriptResponse);
  });
  page.on("pageerror", (error) => {
    const pageErrorClass = LiveCountReporter.pageErrorClass(error);
    if (evidence.pageErrors.length < LiveCountReporter.MAX_PAGE_ERROR_EVENTS) {
      evidence.pageErrors.push(pageErrorClass);
    } else {
      evidence.pageErrorsTruncated = true;
    }
    if (evidence.pageErrorObjects.length < LiveCountReporter.MAX_PAGE_ERROR_EVENTS) {
      evidence.pageErrorObjects.push(error);
    }
    else evidence.pageErrorsTruncated = true;
  });
  evidence.pageErrorCollectorReady = true;
  page.on("requestfailed", (request) => {
    const parsed = new URL(request.url());
    const errorText = request.failure()?.errorText ?? "failed";
    const headers = request.headers();
    const isExpectedNextPrefetchAbort = (
      errorText === "net::ERR_ABORTED"
      && request.method() === "GET"
      && parsed.origin === new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").origin
      && (
        parsed.searchParams.has("_rsc")
        || headers.rsc === "1"
        || headers["next-router-prefetch"] === "1"
        || headers["next-router-segment-prefetch"] === "1"
      )
    );
    const isExpectedGoogleAdSensePingAbort = (
      errorText === "net::ERR_ABORTED"
      && request.method() === "POST"
      && parsed.hostname === "pagead2.googlesyndication.com"
      && parsed.pathname === "/pagead/ping"
    );
    const isExpectedNavigationApiCancellation = (
      errorText === "Load request cancelled"
      && request.method() === "GET"
      && parsed.origin === new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").origin
      && [
        "/api/v1/content/support/status",
        "/api/v1/users/me",
      ].includes(parsed.pathname)
    );
    const isExpectedWebKitPrefetchCancellation = (
      browserName === "webkit"
      && errorText === "Load request cancelled"
      && request.method() === "GET"
      && !request.isNavigationRequest()
      && parsed.origin === new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").origin
      && expectedWebKitPrefetchCancellationPaths.has(parsed.pathname)
    );
    if (
      isExpectedNextPrefetchAbort
      || isExpectedGoogleAdSensePingAbort
      || isExpectedNavigationApiCancellation
      || isExpectedWebKitPrefetchCancellation
    ) {
      return;
    }
    const start = evidence.requestFailureStarts.get(request);
    const failureAt = performance.now();
    const failureNavigationEpoch = evidence.mainFrameNavigationEpoch;
    const baseUrl = process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1";
    const failureClass = classifyBrowserRequestFailure(errorText);
    const intent = evidence.navigationIntent;
    let failureRouteScope: BrowserRequestFailureEventV2["failure_route_scope"] = "unknown";
    try {
      failureRouteScope = browserRequestRouteScope(request.frame().url());
    } catch {
      // Detached frames keep an explicit unknown route in closed evidence.
    }
    const cancellation: BrowserNavigationCancellation | null = intent ? {
      intentToken: intent.token,
      method: request.method(),
      resourceType: request.resourceType(),
      failureClass,
      sameOrigin: start?.sourceDocument
        ? browserRequestSameOrigin(request.url(), baseUrl)
          && browserRequestSameOrigin(request.url(), start.sourceDocument)
        : false,
      mainFrameAtStart: start?.mainFrame ?? false,
      navigationRequest: request.isNavigationRequest(),
      requestStartedAt: start?.startedAt ?? null,
      failedAt: failureAt,
      sourceDocument: start?.sourceDocument ?? null,
      sourceNavigationEpoch: start?.sourceNavigationEpoch ?? null,
    } : null;
    if (intent && start && cancellation && isPotentialNavigationTeardownCancellation(cancellation, intent)) {
      if (evidence.pendingNavigationCancellations.length < MAX_BROWSER_REQUEST_FAILURE_EVENTS) {
        evidence.pendingNavigationCancellations.push({
          request,
          failureClass,
          failureRouteScope,
          failureAt,
          failureNavigationEpoch,
          cancellation,
          start,
          intent,
        });
        return;
      }
    }
    recordUnexpectedBrowserRequestFailure(
      evidence,
      request,
      failureClass,
      start,
      intent,
      failureRouteScope,
      failureAt,
      failureNavigationEpoch,
    );
  });
  await page.exposeBinding("__platformRecordCspViolation", (_source, violation) => {
    evidence.cspViolations.push(violation as BrowserEvidence["cspViolations"][number]);
  });
  await page.addInitScript(() => {
    const target = globalThis as typeof globalThis & {
      __platformRecordCspViolation?: (value: {
        blockedURI: string;
        disposition: string;
        effectiveDirective: string;
        documentURI: string;
        sourceFile: string;
        lineNumber: number;
        columnNumber: number;
      }) => Promise<void>;
      __platformCspViolations?: Array<{
        blockedURI: string;
        effectiveDirective: string;
      }>;
      __platformCspViolationsTruncated?: boolean;
      __platformCspViolationChars?: number;
    };
    target.__platformCspViolations = [];
    target.__platformCspViolationsTruncated = false;
    target.__platformCspViolationChars = 0;
    document.addEventListener("securitypolicyviolation", (event) => {
      const summarizeLocation = (value: string) => {
        try {
          const parsed = new URL(value);
          return `${parsed.origin}${parsed.pathname}`;
        } catch {
          return value.length <= 160 ? value : `${value.slice(0, 157)}...`;
        }
      };
      let blockedURI = event.blockedURI;
      try {
        const parsed = new URL(blockedURI);
        blockedURI = `${parsed.origin}${parsed.pathname}`;
      } catch {
        blockedURI = ["inline", "eval", "blob", "data"].includes(blockedURI)
          ? blockedURI
          : "invalid";
      }
      const violation = {
        blockedURI,
        disposition: event.disposition,
        effectiveDirective: event.effectiveDirective,
        documentURI: summarizeLocation(event.documentURI),
        sourceFile: summarizeLocation(event.sourceFile),
        lineNumber: event.lineNumber,
        columnNumber: event.columnNumber,
      };
      if (
        (violation.effectiveDirective === "img-src" || violation.effectiveDirective === "default-src")
        && violation.blockedURI.length <= 2_048
        && (target.__platformCspViolationChars ?? 0) + violation.blockedURI.length <= 32 * 1024
        && (target.__platformCspViolations?.length ?? 0) < 64
      ) {
        target.__platformCspViolations?.push({
          blockedURI: violation.blockedURI,
          effectiveDirective: violation.effectiveDirective,
        });
        target.__platformCspViolationChars = (
          target.__platformCspViolationChars ?? 0
        ) + violation.blockedURI.length;
      } else if (
        violation.effectiveDirective === "img-src"
        || violation.effectiveDirective === "default-src"
      ) {
        target.__platformCspViolationsTruncated = true;
      }
      void target.__platformRecordCspViolation?.(violation);
    });
  });
});

test.afterEach(async ({ page }, testInfo) => {
  const evidence = browserEvidence.get(page);
  if (evidence) {
    for (const pending of evidence.pendingNavigationCancellations.splice(0)) {
      recordUnexpectedBrowserRequestFailure(
        evidence,
        pending.request,
        pending.failureClass,
        pending.start,
        pending.intent,
        pending.failureRouteScope,
        pending.failureAt,
        pending.failureNavigationEpoch,
      );
    }
  }
  if (evidence) await attachPageErrorEvidence(page, testInfo, evidence);
  if (testInfo.annotations.some((annotation) => annotation.type === "expected-csp-violations")) {
    return;
  }
  if (!evidence) {
    expect([], "SecurityPolicyViolationEvent entries").toEqual([]);
    expect([], "CSP console errors").toEqual([]);
    expect([], "uncaught page errors").toEqual([]);
    expect([], "failed browser requests").toEqual([]);
    expect([], "auth documents and Turnstile belong to the separate human gate").toEqual([]);
    return;
  }
  const attachFailureEvidence = async () => {
    const state: BrowserFailureAttachmentState = {
      imageInventoryRoutes: evidence.imageInventoryRoutes,
      attachedImageInventoryRoutes: evidence.attachedImageInventoryRoutes,
      requestFailureEvidence: {
        schema: 2,
        coverage: "since_before_each",
        failure_phase: "unknown",
        truncated: evidence.requestFailureEventsTruncated,
        events: evidence.requestFailureEvents,
      },
      requestFailureEvidenceAttached: evidence.requestFailureEvidenceAttached,
    };
    await finalizeBrowserFailureEvidence({
      attach: (name, options) => testInfo.attach(name, options),
      state,
      runAssertions: () => {
        expect(evidence.cspViolations, "SecurityPolicyViolationEvent entries").toEqual([]);
        expect(evidence.consoleCspErrors, "CSP console errors").toEqual([]);
        expect(evidence.pageErrors, "uncaught page errors").toEqual([]);
        expect(evidence.unexpectedRequestFailureCount, "failed browser requests").toBe(0);
        expect(
          evidence.unexpectedAuthAutomationRequests,
          "auth documents and Turnstile belong to the separate human gate",
        ).toEqual([]);
      },
      bodyFailureState: evidence.imageBodyFailureObserved
        ? "failed"
        : testInfo.errors.length === 0 && testInfo.status === testInfo.expectedStatus
          ? "passed"
          : "unknown",
    });
    evidence.requestFailureEvidenceAttached = state.requestFailureEvidenceAttached;
  };
  await attachFailureEvidence();
});

const routes = [
  // Auth documents are covered by HTTP-only deploy smoke and the separate
  // human Turnstile gate; a production automation browser must not load them.
  { path: "/", text: "Последние патчи Deadlock" },
  { path: "/info", text: "Как пользоваться" },
  { path: "/tournaments", text: "Deadlock-турниры" },
  { path: "/tournaments/new", text: "Создать турнир" },
  { path: "/profile/me", text: "Профиль игрока" },
  { path: "/profile/lisalexy", text: "Профиль игрока" }
] as const;

for (const route of routes) {
  test(`live route ${route.path} renders without horizontal overflow`, async ({ page }) => {
    await page.goto(route.path);

    await expect(page.getByRole("banner")).toBeVisible();
    await expect(page.getByRole("heading", { name: route.text, exact: true }).first()).toBeVisible();
    await expect(page.locator("body")).toHaveCSS("font-family", /serif|system|Arial|Inter/i);
    await expectNoHorizontalOverflow(page);
  });
}

test("production Chromium live QA keeps its process sandbox enabled", async ({ page, browserName }, testInfo) => {
  test.skip(
    !/^https:\/\/old-sparky\.com\/?$/u.test(
      process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "",
    ),
    "Production-only process sandbox assertion.",
  );
  if (browserName !== "chromium") {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `chromium_only:${testInfo.project.name}`,
    });
    test.skip(true, "Chromium-only process sandbox assertion.");
  }
  await assertLiveQaChromiumSandbox(page.context(), browserName);
});

function safeCspRoute(rawUrl: string, expectedOrigin: string): {
  result_route: "root" | "info" | "other";
  query_present: boolean;
  hash_present: boolean;
  trailing_slash: boolean;
} {
  try {
    const parsed = new URL(rawUrl);
    return {
      result_route: parsed.origin === expectedOrigin
        ? parsed.pathname === "/" ? "root" : parsed.pathname === "/info" ? "info" : "other"
        : "other",
      query_present: parsed.search.length > 0,
      hash_present: parsed.hash.length > 0,
      trailing_slash: parsed.pathname.length > 1 && parsed.pathname.endsWith("/"),
    };
  } catch {
    return {
      result_route: "other",
      query_present: false,
      hash_present: false,
      trailing_slash: false,
    };
  }
}

async function safeDocumentNonceState(
  page: import("@playwright/test").Page,
  expectedNonce: string,
): Promise<"missing" | "stable" | "changed"> {
  const nonces = await page.evaluate(() => ({
    scripts: Array.from(document.querySelectorAll("script:not([src])"))
      .map((element) => (element as HTMLScriptElement).nonce),
    styles: Array.from(document.querySelectorAll("style"))
      .map((element) => (element as HTMLStyleElement).nonce),
  }));
  if (nonces.scripts.length === 0) return "missing";
  return nonces.scripts.every((value) => value === expectedNonce)
    && nonces.styles.every((value) => value === expectedNonce)
    ? "stable" : "changed";
}

test("live CSP nonce is stable on soft navigation and rotates on hard reload", async ({ page }, testInfo) => {
  let expectedOrigin = "";
  let resultRoute: "root" | "info" | "other" = "root";
  let queryPresent = false;
  let hashPresent = false;
  let trailingSlash = false;
  let clickDispatched = false;
  let documentResponse: "not_seen" | "2xx" | "non_2xx" | "failed" = "not_seen";
  let clickDefaultPrevented: "unavailable" | "true" | "false" = "unavailable";
  let infoRscResponse: "not_seen" | "pending" | "2xx" | "non_2xx" | "failed" = "not_seen";
  let infoRscResponsePhase: "not_seen" | "before_click" | "during_click" | "after_click" = "not_seen";
  let infoRscResponseTiming: "unavailable" | "<100ms" | "<1s" | "<5s" | ">=5s" = "unavailable";
  let clickStartedAt: number | null = null;
  let clickFinishedAt: number | null = null;
  let mainFrameCommit: "not_seen" | "info" | "other_origin" | "failed" = "not_seen";
  let softNonce: "not_read" | "missing" | "stable" | "changed" = "not_read";
  let reloadNonce: "not_read" | "missing" | "rotated" | "unchanged" = "not_read";
  const attachment = async () => {
    try {
      await testInfo.attach("live-navigation-failure-v1", {
        body: Buffer.from(JSON.stringify({
          schema: 1,
          test_id: "csp_nonce_rotation",
          result_route: resultRoute,
          query_present: queryPresent,
          hash_present: hashPresent,
          trailing_slash: trailingSlash,
          click_dispatched: clickDispatched,
          document_response: documentResponse,
          click_default_prevented: clickDefaultPrevented,
          info_rsc_response: infoRscResponse,
          info_rsc_response_phase: infoRscResponsePhase,
          info_rsc_response_timing: infoRscResponseTiming,
          main_frame_commit: mainFrameCommit,
          soft_nonce: softNonce,
          reload_nonce: reloadNonce,
        }), "ascii"),
        contentType: "application/json",
      });
    } catch {
      // Diagnostic failure must never replace the CSP assertion result.
    }
  };
  try {
    const initialResponse = await page.goto("/");
    expect(initialResponse).not.toBeNull();
    const initialNonce = responseCspNonce(initialResponse!);
    expectedOrigin = new URL(initialResponse!.url()).origin;
    await expectDocumentNonce(page, initialNonce);

    const clickStateKey = "__oldSparkyPublicQaCspClickV1";
    try {
      await page.evaluate((key) => {
        const state = window as unknown as Record<string, unknown>;
        state[key] = null;
        window.addEventListener("click", (event) => {
          const target = event.target;
          if (!(target instanceof Element)) return;
          const anchor = target.closest("a[href='/info']");
          if (anchor) state[key] = event.defaultPrevented ? "true" : "false";
        }, { capture: false, once: true });
      }, clickStateKey);
    } catch {
      clickDefaultPrevented = "unavailable";
    }

    const responsePhase = (at: number): "before_click" | "during_click" | "after_click" => (
      clickStartedAt === null || at < clickStartedAt
        ? "before_click"
        : clickFinishedAt === null ? "during_click" : "after_click"
    );
    const responseTiming = (at: number): typeof infoRscResponseTiming => {
      if (clickStartedAt === null || at < clickStartedAt) return "unavailable";
      const elapsed = at - clickStartedAt;
      if (elapsed < 100) return "<100ms";
      if (elapsed < 1_000) return "<1s";
      if (elapsed < 5_000) return "<5s";
      return ">=5s";
    };
    const isInfoRsc = (rawUrl: string): boolean => {
      try {
        const parsed = new URL(rawUrl);
        return parsed.origin === expectedOrigin
          && parsed.pathname === "/info"
          && parsed.searchParams.has("_rsc");
      } catch {
        return false;
      }
    };
    const onResponse = (response: import("@playwright/test").Response) => {
      const observedAt = performance.now();
      try {
        const request = response.request();
        const parsed = new URL(response.url());
        if (
          request.isNavigationRequest()
          && parsed.origin === expectedOrigin
          && parsed.pathname === "/info"
        ) documentResponse = response.status() >= 200 && response.status() < 300 ? "2xx" : "non_2xx";
        if (
          isInfoRsc(response.url())
          && (infoRscResponse === "not_seen" || infoRscResponse === "pending")
        ) {
          infoRscResponse = response.status() >= 200 && response.status() < 300 ? "2xx" : "non_2xx";
          infoRscResponsePhase = responsePhase(observedAt);
          infoRscResponseTiming = responseTiming(observedAt);
        }
      } catch {
        // Opaque or detached response objects contribute no diagnostic detail.
      }
    };
    const onRequest = (request: import("@playwright/test").Request) => {
      if (!isInfoRsc(request.url()) || infoRscResponse !== "not_seen") return;
      const observedAt = performance.now();
      infoRscResponse = "pending";
      infoRscResponsePhase = responsePhase(observedAt);
      infoRscResponseTiming = "unavailable";
    };
    const onFrameNavigated = (frame: import("@playwright/test").Frame) => {
      if (frame !== page.mainFrame()) return;
      try {
        const parsed = new URL(frame.url());
        mainFrameCommit = parsed.origin !== expectedOrigin
          ? "other_origin" : parsed.pathname === "/info" ? "info" : "not_seen";
      } catch {
        mainFrameCommit = "other_origin";
      }
    };
    const onRequestFailed = (request: import("@playwright/test").Request) => {
      try {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
          try {
            const parsed = new URL(request.url());
            if (parsed.origin === expectedOrigin && parsed.pathname === "/info") {
              documentResponse = "failed";
            }
          } catch {
            documentResponse = "failed";
          }
        } else if (isInfoRsc(request.url())) {
          infoRscResponse = "failed";
          infoRscResponsePhase = responsePhase(performance.now());
          infoRscResponseTiming = "unavailable";
        }
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
          mainFrameCommit = "failed";
        }
      } catch {
        mainFrameCommit = "failed";
      }
    };
    page.on("response", onResponse);
    page.on("request", onRequest);
    page.on("framenavigated", onFrameNavigated);
    page.on("requestfailed", onRequestFailed);
    try {
      clickStartedAt = performance.now();
      await page.locator('a[href="/info"]').first().click();
      clickFinishedAt = performance.now();
      clickDispatched = true;
      try {
        clickDefaultPrevented = await page.evaluate((key) => {
          const state = window as unknown as Record<string, unknown>;
          const value = state[key];
          delete state[key];
          return value === "true" || value === "false" ? value : "unavailable";
        }, clickStateKey) as typeof clickDefaultPrevented;
      } catch {
        clickDefaultPrevented = "unavailable";
      }
      const route = safeCspRoute(page.url(), expectedOrigin);
      resultRoute = route.result_route;
      queryPresent = route.query_present;
      hashPresent = route.hash_present;
      trailingSlash = route.trailing_slash;
      try {
        softNonce = await safeDocumentNonceState(page, initialNonce);
      } catch {
        softNonce = "not_read";
      }
      await expect(page).toHaveURL(/\/info$/u);
      await expectDocumentNonce(page, initialNonce);
    } finally {
      page.off("response", onResponse);
      page.off("request", onRequest);
      page.off("framenavigated", onFrameNavigated);
      page.off("requestfailed", onRequestFailed);
    }

    const reloadedResponse = await page.reload();
    expect(reloadedResponse).not.toBeNull();
    reloadNonce = safeResponseNonceRotation(reloadedResponse!, initialNonce);
    const reloadedNonce = responseCspNonce(reloadedResponse!);
    await expectDocumentNonce(page, reloadedNonce);
  } catch (error) {
    await attachment();
    throw error;
  }
});

test("local enforced CSP blocks negative inline and external probes", async ({ page }, testInfo) => {
  if (process.env.PLATFORM_LOCAL_CSP_NEGATIVE_QA !== "1") {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `local_csp_disabled:${testInfo.project.name}`,
    });
    test.skip(true, "PLATFORM_LOCAL_CSP_NEGATIVE_QA=1 is required.");
  }
  if (testInfo.project.name !== "live-desktop") {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `desktop_only:${testInfo.project.name}`,
    });
    test.skip(true, "One Chromium enforcement probe is sufficient.");
  }
  const base = new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1");
  expect(["127.0.0.1", "::1", "localhost"]).toContain(base.hostname);
  testInfo.annotations.push({ type: "expected-csp-violations" });
  let forbiddenRequestCount = 0;
  page.on("request", (request) => {
    if (request.url().startsWith("https://csp-smoke.invalid/")) {
      forbiddenRequestCount += 1;
    }
  });

  const response = await page.goto("/");
  expect(response).not.toBeNull();
  expect(response!.headers()["content-security-policy"]).toBeTruthy();
  expect(response!.headers()["content-security-policy-report-only"]).toBeFalsy();
  const result = await page.evaluate(async () => {
    const state = globalThis as typeof globalThis & {
      __cspInlineHandlerRan?: boolean;
      __cspInlineScriptRan?: boolean;
    };
    state.__cspInlineHandlerRan = false;
    state.__cspInlineScriptRan = false;

    const inlineScript = document.createElement("script");
    inlineScript.textContent = "globalThis.__cspInlineScriptRan = true";
    document.body.append(inlineScript);

    const handler = document.createElement("button");
    handler.setAttribute("onclick", "globalThis.__cspInlineHandlerRan = true");
    document.body.append(handler);
    handler.click();

    const styled = document.createElement("div");
    styled.setAttribute("style", "position:absolute");
    document.body.append(styled);

    const external = document.createElement("script");
    external.src = "https://csp-smoke.invalid/probe.js";
    document.body.append(external);
    await new Promise((resolve) => setTimeout(resolve, 250));

    return {
      handlerRan: state.__cspInlineHandlerRan,
      inlineScriptRan: state.__cspInlineScriptRan,
      styledPosition: getComputedStyle(styled).position,
    };
  });
  expect(result).toEqual({
    handlerRan: false,
    inlineScriptRan: false,
    styledPosition: "absolute",
  });
  expect(forbiddenRequestCount).toBe(0);
  await expect.poll(async () => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __platformCspViolations?: Array<{ effectiveDirective: string }>;
    }).__platformCspViolations?.map((value) => value.effectiveDirective) ?? []
  ))).toEqual(expect.arrayContaining([
    "script-src-elem",
    "script-src-attr",
  ]));
  await expect.poll(async () => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __platformCspViolations?: Array<{ effectiveDirective: string }>;
    }).__platformCspViolations?.map((value) => value.effectiveDirective) ?? []
  ))).not.toContain("style-src-attr");
});

test("live tournaments hub exposes a valid empty or populated list", async ({ page }) => {
  await page.goto("/tournaments");

  const cards = page.getByTestId("tournament-card");
  const emptyState = page.locator(".tournament-list-state");
  await expect(cards.first().or(emptyState)).toBeVisible();
  const cardCount = await cards.count();
  if (cardCount === 0) {
    await expect(emptyState).toContainText("Турниров пока нет");
    await expect(page.getByTestId("shown-count")).toHaveText("Показано 0 турниров");
    return;
  }

  await waitForTournamentCardAssets(page);
  await expect(page.getByLabel("Выбор языка")).toHaveCount(0);
  await expect(cards.first().getByRole("heading")).toBeVisible();

  await expect(cards.first()).toHaveAttribute("href", /^\/tournaments\/[a-z0-9-]+$/i);
});

test("live 1920 catalog stays contained after every card asset loads", async ({ page }, testInfo) => {
  if (testInfo.project.name !== "live-desktop") {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `desktop_only:${testInfo.project.name}`,
    });
    test.skip(true, "The visual artifact has one canonical desktop viewport.");
  }
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/tournaments");

  const cards = page.getByTestId("tournament-card");
  await expect(cards.first().or(page.locator(".tournament-list-state"))).toBeVisible();
  const cardCount = await cards.count();
  if (cardCount === 0) {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `empty_tournament_list:${testInfo.project.name}`,
    });
    test.skip(true, "The production tournament list is intentionally empty.");
  }
  await waitForTournamentCardAssets(page, true);
  await page.locator(".hero-wrap").scrollIntoViewIfNeeded();

  const gridBox = await page.getByTestId("tournaments-grid").boundingBox();
  const mainBox = await page.locator("main.main").boundingBox();
  expect(gridBox).not.toBeNull();
  expect(mainBox).not.toBeNull();
  expect(gridBox!.x).toBeGreaterThanOrEqual(mainBox!.x - 1);
  expect(gridBox!.x + gridBox!.width).toBeLessThanOrEqual(mainBox!.x + mainBox!.width + 1);
  if (cardCount >= 3) {
    const firstRowTops = await cards.evaluateAll((items) => (
      items.slice(0, 3).map((item) => Math.round(item.getBoundingClientRect().top))
    ));
    expect(new Set(firstRowTops).size).toBe(1);
  }
  const sharedCoverSources = await cards.evaluateAll((items) => items
    .map((item) => item.querySelector<HTMLImageElement>(".card-banner-media")?.currentSrc ?? "")
    .filter((source) => source.includes("/assets/tournament-covers/")));
  expect(sharedCoverSources.length).toBeGreaterThan(0);
  expect(sharedCoverSources.every((source) => source.includes("rev=20260725-2"))).toBe(true);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    animations: "allow",
    caret: "initial",
    fullPage: true,
    path: testInfo.outputPath("production-tournaments-1920-loaded.png")
  });
});

test("live tournament detail and bracket routes render from the current public data", async ({ page }, testInfo) => {
  await page.goto("/tournaments");
  const firstDetailsLink = page.getByTestId("tournament-card").first();
  await expect(firstDetailsLink.or(page.locator(".tournament-list-state"))).toBeVisible();
  if ((await firstDetailsLink.count()) === 0) {
    testInfo.annotations.push({
      type: "public-live-qa-expected-skip",
      description: `empty_tournament_list:${testInfo.project.name}`,
    });
    test.skip(true, "The production tournament list is intentionally empty.");
  }
  const href = await firstDetailsLink.getAttribute("href");
  expect(href).toBeTruthy();

  await page.goto(href!);
  await expect(page.getByText("Описание", { exact: true })).toBeVisible();
  const bracketLink = page.getByRole("link", { name: "Перейти к сетке" });
  const bracketHref = await bracketLink.getAttribute("href");
  expect(bracketHref).toBeTruthy();
  const detailUrl = new URL(href!, "https://old-sparky.com");
  const bracketUrl = new URL(bracketHref!, "https://old-sparky.com");
  expect(bracketUrl.pathname).toBe(`${detailUrl.pathname}/bracket`);
  expect([...bracketUrl.searchParams.keys()].every((key) => key === "invite_code")).toBe(true);
  if (bracketUrl.searchParams.has("invite_code")) {
    expect(bracketUrl.searchParams.get("invite_code")).toMatch(/^[A-Z0-9]+$/u);
  }
  await expectNoHorizontalOverflow(page);

  await page.goto(`${bracketUrl.pathname}${bracketUrl.search}`);
  await expect(
    page.getByTestId("bracket-shell").or(page.getByTestId("bracket-empty"))
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("live admin route is protected for anonymous users", async ({ page, request }) => {
  const platformOpsResponse = await request.get("/platform-ops", { maxRedirects: 0 });
  expect(platformOpsResponse.status()).toBe(302);
  const accessLocation = platformOpsResponse.headers().location;
  expect(accessLocation).toBeTruthy();
  expect(new URL(accessLocation!, "https://old-sparky.com").hostname).toMatch(
    /\.cloudflareaccess\.com$/u
  );

  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "404", exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-console")).toHaveCount(0);
  await page.waitForLoadState("networkidle");
});

test("live home uses text-only tournament steps without overflow", async ({ page }, testInfo) => {
  await page.goto("/");

  const cards = page.locator(".home-flow-card");
  await expect(cards).toHaveCount(3);
  await expect(cards.getByRole("heading", { level: 3 })).toHaveText([
    "Выбери турнир",
    "Подтверди готовность",
    "Попади в команду"
  ]);
  await expect(cards.locator("img")).toHaveCount(0);
  const homeGeometry = await page.evaluate(() => {
    const hero = document.querySelector<HTMLElement>(".hero-wrap-home")!.getBoundingClientRect();
    const title = document.querySelector<HTMLElement>(".hero-wrap-home .hero-title")!.getBoundingClientRect();
    const subtitle = document.querySelector<HTMLElement>(".hero-wrap-home .hero-subtitle")!.getBoundingClientRect();
    const actions = document.querySelector<HTMLElement>(".hero-actions")!.getBoundingClientRect();
    const card = document.querySelector<HTMLElement>(".home-flow-card")!;
    const number = card.querySelector<HTMLElement>(".home-flow-number")!;
    const numberStyle = getComputedStyle(number);
    return {
      cardHeight: Math.round(card.getBoundingClientRect().height),
      heroGaps: [title.top - hero.top, actions.top - subtitle.bottom, hero.bottom - actions.bottom],
      numberBackground: numberStyle.backgroundImage,
      numberBorder: numberStyle.borderTopWidth,
      numberShadow: numberStyle.boxShadow
    };
  });
  expect(Math.max(...homeGeometry.heroGaps) - Math.min(...homeGeometry.heroGaps)).toBeLessThanOrEqual(1);
  expect(homeGeometry.cardHeight).toBe((page.viewportSize()?.width ?? 0) <= 520 ? 94 : 104);
  expect(homeGeometry.numberBackground).toBe("none");
  expect(homeGeometry.numberBorder).toBe("0px");
  expect(homeGeometry.numberShadow).toBe("none");
  await expectNoHorizontalOverflow(page);
  if (testInfo.project.name !== "live-webkit-mobile") {
    await page.screenshot({
      animations: "allow",
      caret: "initial",
      fullPage: true,
      path: testInfo.outputPath("production-home-density-release.png")
    });
  }
});

test("live public contact surfaces do not publish the support recipient", async ({ request }) => {
  const responses = await Promise.all([
    request.get("/.well-known/security.txt"),
    request.get("/privacy"),
    request.get("/terms")
  ]);
  expect(responses.every((response) => response.ok())).toBe(true);
  const bodies = await Promise.all(responses.map((response) => response.text()));
  expect(bodies[0]).toContain("Contact: https://old-sparky.com/info#support");
  expect(bodies.every((body) => !body.includes("support@old-sparky.com"))).toBe(true);
  expect(bodies.every((body) => !body.includes("mailto:"))).toBe(true);
});

test("live current patch route and objective source icons remain available", async ({ page, request }, testInfo) => {
  const patchId = await currentIndexedPatchId(request);
  const detailResponsePromise = page.waitForResponse((response) => (
    new URL(response.url()).pathname === `/api/v1/content/patches/${patchId}`
  ));
  await page.goto(`/patches/${patchId}`);
  const detailResponse = await detailResponsePromise;
  expect(detailResponse.ok()).toBe(true);
  await expect(page.locator(".patch-article")).toBeVisible();
  for (const iconUrl of [
    "https://assets-bucket.deadlock-api.com/assets-api-res/icons/minimap/soul_jar_marker_psd.png",
    "https://assets-bucket.deadlock-api.com/assets-api-res/icons/minimap/minimap_icon_koth.png",
  ]) {
    const iconResponse = await request.get(iconUrl);
    expect(iconResponse.ok()).toBe(true);
    expect(iconResponse.headers()["content-type"]).toMatch(/^image\/png(?:;|$)/u);
    const iconBytes = await iconResponse.body();
    expect(iconBytes.byteLength).toBeGreaterThan(8);
    expect(iconBytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  }
  await expectNoHorizontalOverflow(page);
  if (testInfo.project.name === "live-desktop") {
    await takeObjectiveScreenshotWithDiagnostics(page, testInfo, () => ({
      animations: "allow",
      caret: "initial",
      fullPage: false,
      path: testInfo.outputPath("production-current-patch.png")
    }));
  }
});

for (const slotIndex of [0, 1, 2, 3] as const) {
  const slotNumber = slotIndex + 1;
  test(`live current patch slot ${slotNumber} DTO renders owner-scoped hero and ability changes`, async ({ page, request }, testInfo) => {
    test.setTimeout(120_000);
    const patchIds = await currentIndexedPatchIds(request);
    expect(patchIds.length).toBeGreaterThan(0);
    expect(patchIds.length).toBeLessThanOrEqual(4);
    expect(new Set(patchIds).size).toBe(patchIds.length);
    const patchId = patchIds[slotIndex];
    if (!patchId) {
      testInfo.annotations.push({
        type: "public-live-qa-expected-skip",
        description: `patch_slot_${slotNumber}_absent:${testInfo.project.name}`,
      });
      test.skip(true, `Current patch slot ${slotNumber} is absent.`);
      return;
    }

    const detailResponsePromise = page.waitForResponse((response) => (
      new URL(response.url()).pathname === `/api/v1/content/patches/${patchId}`
    ));
    await page.goto(`/patches/${patchId}`);
    const detailResponse = await detailResponsePromise;
    expect(detailResponse.ok()).toBe(true);
    const patch = await detailResponse.json() as PlatformPatchDetail;
    expect(patch.id).toBe(patchId);
    expect(patchAcceptanceIssues(patch), `Patch ${patchId} failed content acceptance`).toEqual([]);
    await expect(page.locator(".patch-article")).toBeVisible();
    const renderedSections = page.locator(".patch-section");
    await expect(renderedSections).toHaveCount(patch.sections.length);

    for (const [sectionIndex, section] of patch.sections.entries()) {
      const renderedSection = renderedSections.nth(sectionIndex);
      await expect(renderedSection.getByRole("heading", { level: 2 }))
        .toHaveText(sectionHeading(section));
      const displayName = section.hero_display_name?.trim();
      const hasDistinctDisplayName = section.kind === "hero"
        && Boolean(displayName)
        && displayName!.toLocaleLowerCase() !== section.title.trim().toLocaleLowerCase();
      if (hasDistinctDisplayName) {
        await expect(renderedSection.locator(".patch-hero-canonical-name")).toHaveText(section.title);
      } else {
        await expect(renderedSection.locator(".patch-hero-canonical-name")).toHaveCount(0);
      }
      if (section.mode_name) {
        await expect(renderedSection.locator(".patch-mode-context")).toHaveText(section.mode_name);
      } else {
        await expect(renderedSection.locator(".patch-mode-context")).toHaveCount(0);
      }
      if (section.kind === "hero" && section.hero_presentation_kind !== "introduction") {
        await expect(renderedSection.locator(".patch-hero-description")).toHaveCount(0);
      }
      if (section.kind === "hero") {
        const candidates = section.hero_image_candidates ?? [];
        if (candidates.length) {
          expect(candidates.length).toBeLessThanOrEqual(4);
          expect(new Set(candidates).size).toBe(candidates.length);
          expect(candidates[0]).toBe(section.hero_image_url);
        }
        if (section.hero_image_url) {
          const heroImage = renderedSection.locator(".patch-hero-image");
          await expect(heroImage).toHaveCount(1);
          await expectDecodedImage(heroImage, {
            patchId,
            role: "hero",
            sectionIndex,
            elementIndex: null,
          }, testInfo);
          const renderedSource = await heroImage.getAttribute("src");
          const bundledSource = `/assets/heroes/${encodeURIComponent(
            (section.hero_name ?? section.title).replaceAll(" ", "_"),
          )}.png`;
          expect([...candidates, section.hero_image_url, bundledSource]).toContain(renderedSource);
        }
      }

      if (section.objective_icon_url) {
        const objectiveImage = renderedSection.locator(".patch-objective-image");
        await expect(objectiveImage).toHaveCount(1);
        await expect(renderedSection.locator(".patch-objective-fallback")).toHaveCount(0);
        await expect(objectiveImage).toHaveAttribute("src", section.objective_icon_url);
        await expectDecodedImage(objectiveImage, {
          patchId,
          role: "objective",
          sectionIndex,
          elementIndex: null,
        }, testInfo);
      }

      expectCleanPatchChanges(section.changes);
      await expectRenderedPatchChangeList(
        renderedSection.locator(":scope > .patch-change-content"),
        section.changes,
        section.change_groups ?? [],
        section.change_headings ?? [],
        section.change_lists ?? [],
      );

      const abilityCards = renderedSection.locator(".patch-ability");
      await expect(abilityCards).toHaveCount(section.abilities.length);
      for (const [abilityIndex, ability] of section.abilities.entries()) {
        const renderedAbility = abilityCards.nth(abilityIndex);
        await expect(renderedAbility.getByRole("heading", { level: 3 }))
          .toHaveText(ability.display_name || ability.name);
        const iconCount = await renderedAbility.locator(".patch-ability-icon").count();
        const fallbackCount = await renderedAbility.locator(".patch-ability-icon-fallback").count();
        expect(iconCount + fallbackCount).toBe(1);
        if (ability.icon_url) {
          const icon = renderedAbility.locator(".patch-ability-icon");
          await expect(icon).toHaveCount(1);
          await expect(renderedAbility.locator(".patch-ability-icon-fallback")).toHaveCount(0);
          await expect(icon).toHaveAttribute("src", ability.icon_url);
          await expectDecodedImage(icon, {
            patchId,
            role: "ability",
            sectionIndex,
            elementIndex: abilityIndex,
          }, testInfo);
        }

        expectCleanPatchChanges(ability.changes);
        await expectRenderedPatchChangeList(
          renderedAbility.locator(":scope > .patch-change-content"),
          ability.changes,
          ability.change_groups ?? [],
          ability.change_headings ?? [],
          ability.change_lists ?? [],
          4,
        );

        if (ability.presentation_kind !== "introduction" && ability.presentation_kind !== "replacement") {
          expect(ability.description ?? "").toBe("");
          expect(ability.stats ?? []).toHaveLength(0);
          expect(ability.stats_pending ?? false).toBe(false);
          await expect(renderedAbility.locator(".patch-ability-description, .patch-ability-stats"))
            .toHaveCount(0);
        }
      }
    }

    const modeSections = patch.sections.filter((section) => section.mode_name === "Street Brawl");
    const allPatchChanges = patch.sections.flatMap((section) => [
      ...section.changes,
      ...section.abilities.flatMap((ability) => ability.changes),
    ]);
    expect(allPatchChanges.some((change) => change.includes("Street Brawl:"))).toBe(false);
    if (modeSections.length) {
      expect(modeSections.every((section) => (
        section.kind === "hero" && Boolean(section.hero_name)
      ))).toBe(true);
      const modeRows = modeSections.flatMap((section) => [
        ...section.changes,
        ...section.abilities.flatMap((ability) => ability.changes),
      ]);
      expect(new Set(modeRows).size).toBe(modeRows.length);
      if (patchId === "1847920302240420") {
        expect(modeSections.map((section) => section.hero_name)).toEqual([
          "Deadman Danny",
          "Nurse Harrow",
          "Paige",
          "Rat King",
          "Violet",
        ]);
        expect(modeRows).toHaveLength(13);
      }
    }

    const solomon = patch.sections.find((section) => (
      section.kind === "hero" && section.hero_name === "Solomon" && !section.mode_name
    ));
    if (solomon) {
      const movePiece = solomon.abilities.find((ability) => ability.name === "Move Piece");
      if (movePiece) {
        expect(movePiece.icon_url).toContain("chessmaster_movepiece.png");
      }
    }

    const celesteIndex = patch.sections.findIndex((section) => (
      section.kind === "hero"
      && `${section.hero_name ?? ""} ${section.title}`.toLowerCase().includes("celeste")
    ));
    if (celesteIndex >= 0) {
      const celesteSection = renderedSections.nth(celesteIndex);
      await expect(celesteSection.getByRole("heading", { level: 2 })).toHaveText("Celeste");
      await expect(celesteSection.getByText("СЕЛЕСТА", { exact: true })).toHaveCount(0);
      await celesteSection.locator(".patch-ability-icon").first().scrollIntoViewIfNeeded();
      await expect.poll(() => celesteSection.locator(".patch-ability-icon").first().evaluate((image) => (
        (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
    }

    const nurseIndex = patch.sections.findIndex((section) => (
      section.kind === "hero"
      && `${section.hero_name ?? ""} ${section.title}`.toLowerCase().includes("nurse")
    ));
    if (nurseIndex >= 0) {
      const nurseSection = renderedSections.nth(nurseIndex);
      const nurse = patch.sections[nurseIndex];
      expect(nurse).toBeDefined();
      if (patchId === "1847920302240420") {
        await expect(nurseSection.locator(".patch-hero-image")).toHaveAttribute("src", /.+/u);
        await expect(nurseSection.locator(".patch-ability")).toHaveCount(4);
        await expect(page.locator(".general-patch-section")
          .getByText(/(?:Nurse|Медсестра)\s*:/iu)).toHaveCount(0);
        const abilityNames = nurse!.abilities.map((ability) => ability.display_name || ability.name);
        expect(abilityNames).toEqual(expect.arrayContaining([
          expect.stringMatching(/Wretched Vapors|Гнусные пары/i),
          expect.stringMatching(/Healing Needles|Лечебные иглы/i),
        ]));
        const generalRows = page.locator(".general-patch-section .patch-change-content > *");
        for (const ability of nurse!.abilities) {
          for (const change of ability.changes) {
            await expect(generalRows.getByText(change, { exact: true })).toHaveCount(0);
          }
        }
        await nurseSection.locator(".patch-ability-icon").first().scrollIntoViewIfNeeded();
        await expect.poll(() => nurseSection.locator(".patch-ability-icon").first().evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
      } else if (patchId === "1846651706696737") {
        await expect(nurseSection.locator(".patch-hero-image")).toHaveAttribute("src", /.+/u);
        expect(nurse!.abilities).toHaveLength(4);
        expect(nurse!.abilities.every((ability) => ability.presentation_kind === "introduction")).toBe(true);
        expect(nurse!.abilities.every((ability) => Boolean(ability.description?.trim()))).toBe(true);
        expect(nurse!.abilities.every((ability) => (ability.stats?.length ?? 0) > 0)).toBe(true);
        expect(nurse!.abilities.every((ability) => ability.stats_pending === false)).toBe(true);
        await expect(page.locator(".general-patch-section")
          .getByText(/(?:Nurse|Медсестра)\s*:/iu)).toHaveCount(0);
      }
      for (const ability of nurse!.abilities) {
        for (const change of ability.changes) {
          await expect(nurseSection.getByText(change, { exact: true })).toHaveCount(1);
        }
      }
    }

    const midBossIndex = patch.sections.findIndex((section) => section.objective_key === "mid_boss");
    if (midBossIndex >= 0) {
      const midBossSection = renderedSections.nth(midBossIndex);
      await expect(midBossSection.getByRole("heading", { level: 2 })).toHaveText("Mid Boss");
      const midBossImage = midBossSection.locator(".patch-objective-image");
      await midBossImage.scrollIntoViewIfNeeded();
      await expect.poll(() => midBossImage.evaluate((image) => (
        (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
      ))).toBe(true);
      for (const change of patch.sections[midBossIndex].changes) {
        expect(hasOwnerPrefix(change)).toBe(false);
      }
    }

    await expectNoHorizontalOverflow(page);
  });
}

test("live Cloudflare Analytics is conditionally observed without widening CSP", async ({ page }, testInfo) => {
  const scriptResponses: Array<{ status: number; url: string }> = [];
  const rumRequests: string[] = [];
  page.on("response", (response) => {
    if (new URL(response.url()).hostname === "static.cloudflareinsights.com") {
      scriptResponses.push({ status: response.status(), url: response.url() });
    }
  });
  page.on("request", (request) => {
    const parsed = new URL(request.url());
    if (parsed.pathname === "/cdn-cgi/rum") {
      rumRequests.push(`${parsed.origin}${parsed.pathname}`);
    }
  });

  await page.goto("/");
  const analyticsScripts = page.locator(
    'script[src^="https://static.cloudflareinsights.com/"]',
  );
  const injected = (await analyticsScripts.count()) > 0;
  if (injected) {
    await expect.poll(() => scriptResponses.some((response) => (
      response.status >= 200 && response.status < 400
    ))).toBe(true);
    await expect.poll(() => rumRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);
    expect(rumRequests.every((url) => url === `${new URL(page.url()).origin}/cdn-cgi/rum`)).toBe(true);
  }
  await testInfo.attach("analytics-edge-injection", {
    body: JSON.stringify({
      injected,
      rumObserved: rumRequests.length > 0,
      scriptLoaded: scriptResponses.some((response) => response.status < 400),
    }),
    contentType: "application/json",
  });
});

async function waitForImageRouteContent(
  page: import("@playwright/test").Page,
  route: string,
  patchId: string,
): Promise<void> {
  if (route === "/") {
    const contentResponsePromise = page.waitForResponse((response) => (
      new URL(response.url()).pathname.endsWith("/api/v1/content/home")
    ));
    const navigationResponse = await liveQaGoto(page, route);
    expect(navigationResponse?.ok(), "home route should render successfully").toBe(true);
    const contentResponse = await contentResponsePromise;
    expect(contentResponse.ok(), "home content should load successfully").toBe(true);
    await expect(page.locator(".home-content-error")).toHaveCount(0);
    await expect(
      page.locator(
        ".patch-showcase:not(.patch-showcase-loading), .home-content-group .home-empty-state",
      ).first(),
    ).toBeVisible();
    return;
  }

  if (route.startsWith("/patches/")) {
    const contentResponsePromise = page.waitForResponse((response) => (
      new URL(response.url()).pathname.endsWith(`/api/v1/content/patches/${patchId}`)
    ));
    const navigationResponse = await liveQaGoto(page, route);
    expect(navigationResponse?.ok(), "patch route should render successfully").toBe(true);
    const contentResponse = await contentResponsePromise;
    expect(contentResponse.ok(), "patch content should load successfully").toBe(true);
    await expect(page.locator(".home-content-error")).toHaveCount(0);
    await expect(page.locator(".patch-article")).toBeVisible();
    return;
  }

  const navigationResponse = await liveQaGoto(page, route);
  expect(navigationResponse?.ok(), "tournament route should render successfully").toBe(true);
  const tournamentGrid = page.getByTestId("tournaments-grid");
  await expect(tournamentGrid).toBeVisible();
  await expect(tournamentGrid).toHaveAttribute("aria-busy", "false");
  await expect(tournamentGrid.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".tournament-list-error")).toHaveCount(0);
}

test("live image currentSrc inventory stays inside the exact CSP hosts", async ({ page, request }, testInfo) => {
  test.setTimeout(120_000);
  const allowedHosts = new Set([
    new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").hostname,
    "assets-bucket.deadlock-api.com",
    "cdn.old-sparky.com",
    "clan.fastly.steamstatic.com",
    "deadlock.io",
    "i2.ytimg.com",
    "i3.ytimg.com",
    "steamstore-a.akamaihd.net",
  ]);
  const inventory: Array<{ host: string; path: string; route: string }> = [];
  const patchId = await currentIndexedPatchId(request);
  const routes = [
    { path: "/", route: "home" as const },
    { path: `/patches/${patchId}`, route: "patch" as const },
    { path: "/tournaments", route: "tournaments" as const },
  ];
  const imageEvidence: Array<{
    schema: 2;
    route: "home" | "patch" | "tournaments";
    image_count: number;
    missing_current_src_count: number;
    unloaded_count: number;
    complete: boolean;
    counts_truncated: boolean;
    image_entries_truncated: boolean;
    coverage: {
      network_state: "captured" | "truncated" | "unavailable";
      csp_state: "captured" | "truncated" | "unavailable";
      unmatched_csp_count: number;
      csp_events_truncated: boolean;
    };
    images: Array<{
      ordinal: number;
      generation: number;
      src_present: boolean;
      srcset_present: boolean;
      complete: boolean;
      natural_width_positive: boolean;
      current_src_present: boolean;
      loading: "lazy" | "eager" | "unknown";
      viewport_state: "intersecting" | "outside" | "unknown";
      scroll_attempted: boolean;
      request_state: string;
      response_received: boolean | null;
      response_status_class: string;
      request_finished: boolean | null;
      request_failed: boolean | null;
      request_failure_class: string;
      csp_state: string;
      csp_image_block_count: number;
      terminal_duration_bucket: string;
    }>;
  }> = [];
  const pageEvidence = browserEvidence.get(page);
  if (pageEvidence) pageEvidence.imageInventoryRoutes = imageEvidence;
  try {
    for (const { path: routePath, route } of routes) {
      const imageResult = await collectLiveImageInventory(page, {
        route: routePath,
        waitForContent: () => waitForImageRouteContent(page, routePath, patchId),
        timeoutMs: 30_000,
      });
      const images = imageResult.images;
      const missingCurrentSrcCount = images.filter((image) => !image.currentSrc).length;
      const unloadedCount = images.filter((image) => !image.loaded).length;
      const countLimit = 512;
      const diagnosticLimit = 16;
      const boundedImages = images.slice(0, countLimit);
      const routeOrdinals = new Map(
        images.map((image, index) => [image.diagnostic.ordinal, index + 1]),
      );
      const imageEntriesTruncated = images.length > diagnosticLimit
        || boundedImages.length !== images.length;
      const selectedDiagnostics = [...boundedImages]
        .sort((left, right) => (
          Number(!right.currentSrc || !right.loaded) - Number(!left.currentSrc || !left.loaded)
          || left.diagnostic.ordinal - right.diagnostic.ordinal
        ))
        .slice(0, diagnosticLimit)
        .map(({ diagnostic }) => ({
          ordinal: routeOrdinals.get(diagnostic.ordinal) ?? diagnostic.ordinal,
          generation: diagnostic.generation,
          src_present: diagnostic.srcPresent,
          srcset_present: diagnostic.srcSetPresent,
          complete: diagnostic.complete,
          natural_width_positive: diagnostic.naturalWidthPositive,
          current_src_present: diagnostic.currentSrcPresent,
          loading: diagnostic.loading,
          viewport_state: diagnostic.viewportState,
          scroll_attempted: diagnostic.scrollAttempted,
          request_state: diagnostic.requestState,
          response_received: diagnostic.responseReceived,
          response_status_class: diagnostic.responseStatusClass,
          request_finished: diagnostic.requestFinished,
          request_failed: diagnostic.requestFailed,
          request_failure_class: diagnostic.requestFailureClass,
          csp_state: diagnostic.cspState,
          csp_image_block_count: diagnostic.cspImageBlockCount,
          terminal_duration_bucket: diagnostic.terminalDurationBucket,
        }));
      imageEvidence.push({
        schema: 2,
        route,
        image_count: Math.min(images.length, countLimit),
        missing_current_src_count: Math.min(missingCurrentSrcCount, countLimit),
        unloaded_count: Math.min(unloadedCount, countLimit),
        complete: imageResult.complete,
        counts_truncated: images.length > countLimit
          || missingCurrentSrcCount > countLimit
          || unloadedCount > countLimit,
        image_entries_truncated: imageEntriesTruncated,
        coverage: {
          network_state: imageResult.observationCoverage.networkState,
          csp_state: imageResult.observationCoverage.cspState,
          unmatched_csp_count: imageResult.observationCoverage.unmatchedCspCount,
          csp_events_truncated: imageResult.observationCoverage.cspEventsTruncated,
        },
        images: selectedDiagnostics,
      });
      allowedHosts.add(new URL(page.url()).hostname);
      expect(imageResult.complete, `all images load on ${route}`).toBe(true);
      for (const image of images) {
        expect(image.currentSrc).toBeTruthy();
        expect(image.loaded, `image failed on ${route}`).toBe(true);
        const parsed = new URL(image.currentSrc);
        expect(parsed.protocol).not.toBe("data:");
        if (parsed.protocol !== "blob:") {
          expect(allowedHosts.has(parsed.hostname), `unexpected image host ${parsed.hostname}`).toBe(true);
        }
        inventory.push({ host: parsed.hostname, path: parsed.pathname, route: routePath });
      }
    }
    await testInfo.attach("loaded-image-current-src-inventory", {
      body: JSON.stringify(inventory),
      contentType: "application/json",
    });
  } catch (error) {
    if (pageEvidence) pageEvidence.imageBodyFailureObserved = true;
    throw error;
  }
});

function responseCspNonce(response: import("@playwright/test").Response): string {
  const headers = response.headers();
  const enforced = headers["content-security-policy"];
  const reportOnly = headers["content-security-policy-report-only"];
  expect(Boolean(enforced) !== Boolean(reportOnly)).toBe(true);
  const matches = (enforced ?? reportOnly ?? "").match(/'nonce-([^']+)'/gu) ?? [];
  expect(matches).toHaveLength(2);
  const nonces = matches.map((value) => value.slice("'nonce-".length, -1));
  expect(new Set(nonces).size).toBe(1);
  const decoded = Buffer.from(nonces[0], "base64");
  expect(decoded.byteLength).toBeGreaterThanOrEqual(16);
  return nonces[0];
}

function safeResponseNonceRotation(
  response: import("@playwright/test").Response,
  initialNonce: string,
): "missing" | "rotated" | "unchanged" {
  try {
    const headers = response.headers();
    const policy = headers["content-security-policy"]
      ?? headers["content-security-policy-report-only"]
      ?? "";
    const matches = policy.match(/'nonce-([^']+)'/gu) ?? [];
    if (matches.length !== 2) return "missing";
    const nonces = matches.map((value) => value.slice("'nonce-".length, -1));
    if (new Set(nonces).size !== 1 || Buffer.from(nonces[0], "base64").byteLength < 16) {
      return "missing";
    }
    return nonces[0] === initialNonce ? "unchanged" : "rotated";
  } catch {
    return "missing";
  }
}

function summarizeCspConsoleError(message: string): string {
  const directive = message.match(/directive\s+['"]([a-z][a-z0-9-]*)/iu)?.[1]
    ?? "unknown";
  const category = /inline style/iu.test(message)
    ? "inline-style"
    : /inline script/iu.test(message)
      ? "inline-script"
      : /\beval\b/iu.test(message)
        ? "eval"
        : /refused to connect/iu.test(message)
          ? "connect"
          : /refused to load/iu.test(message)
            ? "load"
            : "configuration";
  return `directive=${directive} category=${category}`;
}

async function expectDocumentNonce(
  page: import("@playwright/test").Page,
  nonce: string,
) {
  const state = await page.evaluate(() => ({
    eventHandlers: document.querySelectorAll("[onclick],[onerror],[onload]").length,
    inlineScriptNonces: Array.from(document.querySelectorAll("script:not([src])"))
      .map((element) => (element as HTMLScriptElement).nonce),
    styleNonces: Array.from(document.querySelectorAll("style"))
      .map((element) => (element as HTMLStyleElement).nonce),
  }));
  const appOwnedStyleAttributes = await page.locator("[style]").evaluateAll(
    (elements, vendorSelector) => elements.filter(
      (element) => !element.matches(vendorSelector),
    ).length,
    googleOwnedInlineStyleSelector,
  );
  expect(state.eventHandlers).toBe(0);
  expect(appOwnedStyleAttributes).toBe(0);
  expect(state.inlineScriptNonces.length).toBeGreaterThan(0);
  expect(state.inlineScriptNonces.every((value) => value === nonce)).toBe(true);
  expect(state.styleNonces.every((value) => value === nonce)).toBe(true);
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  const overflow = await page.evaluate(() => {
    const documentElement = document.documentElement;
    return Math.max(0, documentElement.scrollWidth - documentElement.clientWidth);
  });
  expect(overflow).toBeLessThanOrEqual(2);
}

async function waitForTournamentCardAssets(
  page: import("@playwright/test").Page,
  allCards = false
) {
  const cards = page.getByTestId("tournament-card");
  const count = allCards ? await cards.count() : Math.min(1, await cards.count());
  for (let index = 0; index < count; index += 1) {
    const card = cards.nth(index);
    await card.scrollIntoViewIfNeeded();
    await card.evaluate(
      () => new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }),
      undefined,
      { timeout: 2_000 },
    );
    await expect.poll(
      async () => card.evaluate((cardElement) => {
        const images = Array.from(cardElement.querySelectorAll<HTMLImageElement>("img"));
        const backgroundUrls = Array.from(cardElement.querySelectorAll<HTMLElement>(".card-banner"))
          .map((banner) => getComputedStyle(banner).backgroundImage.match(/url\(["']?(.*?)["']?\)/u)?.[1])
          .filter((url): url is string => Boolean(url));
        const target = globalThis as typeof globalThis & {
          __platformCardBackgroundProbes?: Record<string, HTMLImageElement>;
        };
        const probes = target.__platformCardBackgroundProbes ??= {};
        for (const url of backgroundUrls) {
          if (!probes[url]) {
            const probe = new Image();
            probe.src = url;
            probes[url] = probe;
          }
        }
        return images.every((image) => image.complete && image.naturalWidth > 0)
          && (backgroundUrls.length === 0
            || backgroundUrls.every((url) => probes[url].complete && probes[url].naturalWidth > 0));
      }),
      {
        message: `card ${index + 1} images load`,
        timeout: 15_000,
      },
    ).toBe(true);
  }
}
