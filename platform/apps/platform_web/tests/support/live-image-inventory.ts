import type { Page } from "@playwright/test";

export type LiveImageInventoryRow = {
  alt: string;
  currentSrc: string;
  loaded: boolean;
  source: string;
  srcSet: string;
  diagnostic: LiveImageDiagnostic;
};

export type LiveImageDiagnostic = {
  ordinal: number;
  generation: number;
  srcPresent: boolean;
  srcSetPresent: boolean;
  complete: boolean;
  naturalWidthPositive: boolean;
  currentSrcPresent: boolean;
  loading: "lazy" | "eager" | "unknown";
  viewportState: "intersecting" | "outside" | "unknown";
  scrollAttempted: boolean;
  requestState:
    | "not_observed"
    | "pending"
    | "response_received"
    | "request_finished"
    | "request_failed"
    | "ambiguous"
    | "unavailable";
  responseReceived: boolean | null;
  responseStatusClass: "none" | "2xx" | "3xx" | "4xx" | "5xx" | "other" | "ambiguous" | "unavailable";
  requestFinished: boolean | null;
  requestFailed: boolean | null;
  requestFailureClass: "none" | "dns" | "connection" | "timeout" | "tls" | "aborted" | "other" | "ambiguous" | "unavailable";
  cspState: "matched" | "not_observed" | "ambiguous" | "unavailable";
  cspImageBlockCount: number;
  terminalDurationBucket:
    | "<100ms"
    | "<1s"
    | "<5s"
    | ">=5s"
    | "not_observed"
    | "ambiguous"
    | "unavailable";
};

export type LiveImageInventoryResult = {
  images: LiveImageInventoryRow[];
  complete: boolean;
  scrollAttempts: number;
  observationCoverage: {
    networkState: "captured" | "truncated" | "unavailable";
    cspState: "captured" | "truncated" | "unavailable";
    unmatchedCspCount: number;
    cspEventsTruncated: boolean;
  };
};

type CollectLiveImageInventoryOptions = {
  route: string;
  waitForContent: () => Promise<void>;
  timeoutMs?: number;
};

type ImageInventoryWindow = Window & {
  __oldsparkyLiveImageInventory?: {
    ids: WeakMap<HTMLImageElement, number>;
    scrolledSources: WeakMap<HTMLImageElement, string>;
    signatures: WeakMap<HTMLImageElement, string>;
    generations: WeakMap<HTMLImageElement, number>;
    sourceSignatureHistory: WeakMap<HTMLImageElement, Set<string>>;
    currentSrcHistory: WeakMap<HTMLImageElement, Set<string>>;
    sourceHistoryTruncated: WeakSet<HTMLImageElement>;
    sourceJoinAmbiguous: WeakSet<HTMLImageElement>;
    sourceHistoryBytes: number;
    nextId: number;
  };
  __platformCspViolations?: Array<{
    blockedURI: string;
    effectiveDirective: string;
  }>;
  __platformCspViolationsTruncated?: boolean;
};

type ImageRequestObservation = {
  url: string;
  startedAt: number;
  responseStatus: number | null;
  terminalAt: number | null;
  failureClass: LiveImageDiagnostic["requestFailureClass"];
  finished: boolean;
  failed: boolean;
};

const MAX_IMAGE_DIAGNOSTIC_EVENTS = 64;
const MAX_IMAGE_SOURCE_VALUE_CHARS = 2_048;
const MAX_IMAGE_SOURCE_HISTORY = 8;
const MAX_IMAGE_SOURCE_HISTORY_BYTES = 32 * 1024;
const MAX_IMAGE_REQUEST_URL_CHARS = 2_048;
const MAX_IMAGE_REQUEST_URL_BYTES = 32 * 1024;

function imageRequestFailureClass(errorText: string): LiveImageDiagnostic["requestFailureClass"] {
  if (/ERR_NAME_NOT_RESOLVED|ERR_DNS/iu.test(errorText)) return "dns";
  if (/ERR_CONNECTION|ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_CHANGED/iu.test(errorText)) return "connection";
  if (/ERR_TIMED_OUT|ERR_CONNECTION_TIMEOUT/iu.test(errorText)) return "timeout";
  if (/ERR_CERT|ERR_SSL|ERR_TLS/iu.test(errorText)) return "tls";
  if (/ERR_ABORTED|cancelled/iu.test(errorText)) return "aborted";
  return "other";
}

function imageStatusClass(status: number): LiveImageDiagnostic["responseStatusClass"] {
  if (status >= 200 && status < 300) return "2xx";
  if (status >= 300 && status < 400) return "3xx";
  if (status >= 400 && status < 500) return "4xx";
  if (status >= 500 && status < 600) return "5xx";
  return "other";
}

function imageDurationBucket(durationMs: number): LiveImageDiagnostic["terminalDurationBucket"] {
  if (durationMs < 100) return "<100ms";
  if (durationMs < 1_000) return "<1s";
  if (durationMs < 5_000) return "<5s";
  return ">=5s";
}

export async function collectLiveImageInventory(
  page: Page,
  { route, waitForContent, timeoutMs = 30_000 }: CollectLiveImageInventoryOptions,
): Promise<LiveImageInventoryResult> {
  const requests = new Map<import("@playwright/test").Request, ImageRequestObservation>();
  let networkEventsTruncated = false;
  let requestUrlBytes = 0;
  const onRequest = (request: import("@playwright/test").Request) => {
    if (request.resourceType() !== "image") return;
    const url = request.url();
    if (
      requests.size >= MAX_IMAGE_DIAGNOSTIC_EVENTS
      || url.length > MAX_IMAGE_REQUEST_URL_CHARS
      || requestUrlBytes + url.length > MAX_IMAGE_REQUEST_URL_BYTES
    ) {
      networkEventsTruncated = true;
      return;
    }
    requestUrlBytes += url.length;
    requests.set(request, {
      url,
      startedAt: performance.now(),
      responseStatus: null,
      terminalAt: null,
      failureClass: "none",
      finished: false,
      failed: false,
    });
  };
  const onResponse = (response: import("@playwright/test").Response) => {
    const observation = requests.get(response.request());
    if (observation) observation.responseStatus = response.status();
  };
  const onRequestFinished = (request: import("@playwright/test").Request) => {
    const observation = requests.get(request);
    if (observation) {
      observation.finished = true;
      observation.terminalAt = performance.now();
    }
  };
  const onRequestFailed = (request: import("@playwright/test").Request) => {
    const observation = requests.get(request);
    if (observation) {
      observation.failed = true;
      observation.terminalAt = performance.now();
      observation.failureClass = imageRequestFailureClass(request.failure()?.errorText ?? "");
    }
  };
  page.on("request", onRequest);
  page.on("response", onResponse);
  page.on("requestfinished", onRequestFinished);
  page.on("requestfailed", onRequestFailed);
  const removeRequestListeners = () => {
    page.off("request", onRequest);
    page.off("response", onResponse);
    page.off("requestfinished", onRequestFinished);
    page.off("requestfailed", onRequestFailed);
  };

  try {
    await waitForContent();
  } catch (error) {
    removeRequestListeners();
    throw error;
  }
  await page.evaluate(() => {
    const target = window as ImageInventoryWindow;
    target.__oldsparkyLiveImageInventory ??= {
      ids: new WeakMap<HTMLImageElement, number>(),
      scrolledSources: new WeakMap<HTMLImageElement, string>(),
      signatures: new WeakMap<HTMLImageElement, string>(),
      generations: new WeakMap<HTMLImageElement, number>(),
      sourceSignatureHistory: new WeakMap<HTMLImageElement, Set<string>>(),
      currentSrcHistory: new WeakMap<HTMLImageElement, Set<string>>(),
      sourceHistoryTruncated: new WeakSet<HTMLImageElement>(),
      sourceJoinAmbiguous: new WeakSet<HTMLImageElement>(),
      sourceHistoryBytes: 0,
      nextId: 0,
    };
  });

  const deadline = performance.now() + timeoutMs;
  const imageLocator = page.locator("img");
  let previousSignature = "";
  let stablePasses = 0;
  let scrollAttempts = 0;
  let snapshot: Array<{
    id: number;
    generation: number;
    alt: string;
    currentSrc: string;
    loaded: boolean;
    source: string;
    sourceSet: string;
    srcPresent: boolean;
    srcSetPresent: boolean;
    sourceJoinAmbiguous: boolean;
    sourceHistoryTruncated: boolean;
    complete: boolean;
    naturalWidthPositive: boolean;
    loading: "lazy" | "eager" | "unknown";
    viewportState: "intersecting" | "outside" | "unknown";
    scrollAttempted: boolean;
  }> = [];
  let coverage: LiveImageInventoryResult["observationCoverage"] = {
    networkState: "captured",
    cspState: "unavailable",
    unmatchedCspCount: 0,
    cspEventsTruncated: false,
  };
  const observedAt = () => performance.now();
  const captureImages = async (): Promise<LiveImageInventoryRow[]> => {
    const rawCsp = await page.evaluate(() => {
      const target = window as ImageInventoryWindow;
      return {
        events: target.__platformCspViolations ?? null,
        truncated: target.__platformCspViolationsTruncated === true,
      };
    }).catch(() => ({ events: null, truncated: false }));
    const cspEvents = rawCsp.events;
    coverage = {
      networkState: networkEventsTruncated ? "truncated" : "captured",
      cspState: cspEvents === null ? "unavailable" : rawCsp.truncated ? "truncated" : "captured",
      unmatchedCspCount: 0,
      cspEventsTruncated: rawCsp.truncated,
    };
    const currentSrcCounts = new Map<string, number>();
    for (const image of snapshot) {
      if (image.currentSrc) {
        currentSrcCounts.set(image.currentSrc, (currentSrcCounts.get(image.currentSrc) ?? 0) + 1);
      }
    }
    const requestsByUrl = new Map<string, ImageRequestObservation[]>();
    for (const request of requests.values()) {
      const matching = requestsByUrl.get(request.url) ?? [];
      matching.push(request);
      requestsByUrl.set(request.url, matching);
    }
    const currentImageRows = snapshot.map((image) => ({
      image,
      currentImageMatches: currentSrcCounts.get(image.currentSrc) ?? 0,
      matchingRequests: image.currentSrc ? requestsByUrl.get(image.currentSrc) ?? [] : [],
    }));
    const normalizedCspEvents = (cspEvents ?? []).filter((event) => (
      event.effectiveDirective === "img-src" || event.effectiveDirective === "default-src"
    ));
    const cspMatches = new Map<number, number>();
    for (const event of normalizedCspEvents) {
      let blockedUrl: URL | null = null;
      try {
        blockedUrl = new URL(event.blockedURI);
      } catch {
        // A non-URL CSP value cannot be safely joined to an image.
      }
      const matching = blockedUrl
        ? currentImageRows.filter(({ image }) => {
          try {
            const current = new URL(image.currentSrc);
            return current.origin === blockedUrl?.origin && current.pathname === blockedUrl?.pathname;
          } catch {
            return false;
          }
        })
        : [];
      if (
        matching.length === 1
        && !matching[0].image.sourceJoinAmbiguous
        && !matching[0].image.sourceHistoryTruncated
      ) {
        const ordinal = matching[0].image.id;
        cspMatches.set(ordinal, Math.min(
          MAX_IMAGE_DIAGNOSTIC_EVENTS,
          (cspMatches.get(ordinal) ?? 0) + 1,
        ));
      } else {
        coverage.unmatchedCspCount = Math.min(
          MAX_IMAGE_DIAGNOSTIC_EVENTS,
          coverage.unmatchedCspCount + 1,
        );
      }
    }
    const now = observedAt();
    return currentImageRows.map(({ image, currentImageMatches, matchingRequests }) => {
      const uriJoinIsAmbiguous = (
        currentImageMatches > 1
        || matchingRequests.length > 1
        || image.sourceJoinAmbiguous
      );
      let requestState: LiveImageDiagnostic["requestState"];
      let responseReceived: boolean | null;
      let responseStatusClass: LiveImageDiagnostic["responseStatusClass"];
      let requestFinished: boolean | null;
      let requestFailed: boolean | null;
      let requestFailureClass: LiveImageDiagnostic["requestFailureClass"];
      let terminalDurationBucket: LiveImageDiagnostic["terminalDurationBucket"];
      if (!image.currentSrc) {
        requestState = "unavailable";
        responseReceived = null;
        responseStatusClass = "unavailable";
        requestFinished = null;
        requestFailed = null;
        requestFailureClass = "unavailable";
        terminalDurationBucket = "unavailable";
      } else if (uriJoinIsAmbiguous || image.sourceHistoryTruncated || networkEventsTruncated) {
        const unavailable = image.sourceHistoryTruncated || networkEventsTruncated;
        requestState = unavailable ? "unavailable" : "ambiguous";
        responseReceived = null;
        responseStatusClass = unavailable ? "unavailable" : "ambiguous";
        requestFinished = null;
        requestFailed = null;
        requestFailureClass = unavailable ? "unavailable" : "ambiguous";
        terminalDurationBucket = unavailable ? "unavailable" : "ambiguous";
      } else if (matchingRequests.length === 0) {
        requestState = "not_observed";
        responseReceived = false;
        responseStatusClass = "none";
        requestFinished = false;
        requestFailed = false;
        requestFailureClass = "none";
        terminalDurationBucket = "not_observed";
      } else {
        const request = matchingRequests[0];
        responseReceived = request.responseStatus !== null;
        responseStatusClass = responseReceived
          ? imageStatusClass(request.responseStatus as number)
          : "none";
        requestFinished = request.finished;
        requestFailed = request.failed;
        requestFailureClass = request.failed ? request.failureClass : "none";
        requestState = request.failed
          ? "request_failed"
          : request.finished
            ? "request_finished"
            : responseReceived
              ? "response_received"
              : "pending";
        terminalDurationBucket = imageDurationBucket(
          Math.max(0, (request.terminalAt ?? now) - request.startedAt),
        );
      }
      const cspImageBlockCount = cspMatches.get(image.id) ?? 0;
      const cspState: LiveImageDiagnostic["cspState"] = !image.currentSrc
        ? "unavailable"
        : coverage.cspState === "unavailable" || image.sourceHistoryTruncated
        ? "unavailable"
        : currentImageMatches > 1 || image.sourceJoinAmbiguous
          ? "ambiguous"
          : cspImageBlockCount > 0
            ? "matched"
            : coverage.cspEventsTruncated
              ? "ambiguous"
              : "not_observed";
      return {
        alt: image.alt,
        currentSrc: image.currentSrc,
        loaded: image.loaded,
        source: image.source,
        srcSet: image.sourceSet,
        diagnostic: {
          ordinal: image.id,
          generation: image.generation,
          srcPresent: image.srcPresent,
          srcSetPresent: image.srcSetPresent,
          complete: image.complete,
          naturalWidthPositive: image.naturalWidthPositive,
          currentSrcPresent: Boolean(image.currentSrc),
          loading: image.loading,
          viewportState: image.viewportState,
          scrollAttempted: image.scrollAttempted,
          requestState,
          responseReceived,
          responseStatusClass,
          requestFinished,
          requestFailed,
          requestFailureClass,
          cspState,
          cspImageBlockCount,
          terminalDurationBucket,
        },
      };
    });
  };

  while (performance.now() < deadline) {
    const handles = await imageLocator.elementHandles();
    for (const imageHandle of handles) {
      let shouldScroll = false;
      try {
        shouldScroll = await imageHandle.evaluate((element) => {
          const image = element as HTMLImageElement;
          const state = (window as ImageInventoryWindow).__oldsparkyLiveImageInventory;
          if (!state) return false;
          if (!state.ids.has(image)) {
            state.nextId += 1;
            state.ids.set(image, state.nextId);
          }
          const source = `${image.getAttribute("src") ?? ""}\0${image.getAttribute("srcset") ?? ""}`;
          if (state.scrolledSources.get(image) === source) return false;
          state.scrolledSources.set(image, source);
          return true;
        });
        if (shouldScroll) {
          await imageHandle.evaluate((element) => {
            (element as HTMLImageElement).scrollIntoView({ block: "center", inline: "nearest" });
          });
          scrollAttempts += 1;
          await page.waitForTimeout(50);
        }
      } catch {
        // A replaced node is rediscovered on the next bounded pass.
      }
    }

    snapshot = await imageLocator.evaluateAll((elements) => {
      const state = (window as ImageInventoryWindow).__oldsparkyLiveImageInventory;
      return elements.map((element) => {
        const image = element as HTMLImageElement;
        if (state && !state.ids.has(image)) {
          state.nextId += 1;
          state.ids.set(image, state.nextId);
        }
        const signature = `${image.getAttribute("src") ?? ""}\0${image.getAttribute("srcset") ?? ""}\0${image.currentSrc}`;
        let generation = state?.generations.get(image) ?? 1;
        const previous = state?.signatures.get(image);
        let sourceJoinAmbiguous = state?.sourceJoinAmbiguous.has(image) ?? false;
        let sourceHistoryTruncated = state?.sourceHistoryTruncated.has(image) ?? false;
        if (state) {
          const signatureHistory = state.sourceSignatureHistory.get(image) ?? new Set<string>();
          const currentSrcHistory = state.currentSrcHistory.get(image) ?? new Set<string>();
          const sourceChanged = previous !== undefined && previous !== signature;
          if (sourceChanged) {
            generation += 1;
            if (signatureHistory.has(signature) || (image.currentSrc && currentSrcHistory.has(image.currentSrc))) {
              sourceJoinAmbiguous = true;
              state.sourceJoinAmbiguous.add(image);
            }
          }
          const historyBytes = signature.length + image.currentSrc.length;
          if (
            signature.length > MAX_IMAGE_SOURCE_VALUE_CHARS
            || image.currentSrc.length > MAX_IMAGE_SOURCE_VALUE_CHARS
            || signatureHistory.size >= MAX_IMAGE_SOURCE_HISTORY
            || currentSrcHistory.size >= MAX_IMAGE_SOURCE_HISTORY
            || state.sourceHistoryBytes + historyBytes > MAX_IMAGE_SOURCE_HISTORY_BYTES
          ) {
            if (!signatureHistory.has(signature) || !currentSrcHistory.has(image.currentSrc)) {
              sourceHistoryTruncated = true;
              state.sourceHistoryTruncated.add(image);
            }
          } else {
            if (!signatureHistory.has(signature)) state.sourceHistoryBytes += signature.length;
            signatureHistory.add(signature);
            if (image.currentSrc && !currentSrcHistory.has(image.currentSrc)) {
              state.sourceHistoryBytes += image.currentSrc.length;
              currentSrcHistory.add(image.currentSrc);
            }
          }
          state.sourceSignatureHistory.set(image, signatureHistory);
          state.currentSrcHistory.set(image, currentSrcHistory);
        }
        if (state) {
          state.signatures.set(image, signature);
          state.generations.set(image, generation);
        }
        const rect = image.getBoundingClientRect();
        const viewportState = rect.width > 0 && rect.height > 0
          ? rect.bottom >= 0 && rect.right >= 0 && rect.top <= window.innerHeight && rect.left <= window.innerWidth
            ? "intersecting" as const
            : "outside" as const
          : "unknown" as const;
        return {
          id: state?.ids.get(image) ?? 0,
          generation,
          sourceJoinAmbiguous,
          sourceHistoryTruncated,
          alt: image.alt,
          currentSrc: image.currentSrc,
          loaded: image.complete && image.naturalWidth > 0,
          source: image.getAttribute("src") ?? "",
          sourceSet: image.getAttribute("srcset") ?? "",
          srcPresent: image.hasAttribute("src"),
          srcSetPresent: image.hasAttribute("srcset"),
          complete: image.complete,
          naturalWidthPositive: image.naturalWidth > 0,
          loading: image.loading === "lazy" || image.loading === "eager" ? image.loading : "unknown" as const,
          viewportState,
          scrollAttempted: state?.scrolledSources.get(image) === `${image.getAttribute("src") ?? ""}\0${image.getAttribute("srcset") ?? ""}`,
        };
      });
    });
    const signature = snapshot.map((image) => (
      `${image.id}\0${image.source}\0${image.sourceSet}\0${image.currentSrc}\0${image.loaded}`
    )).join("\n");
    const allLoaded = snapshot.length > 0 && snapshot.every((image) => (
      Boolean(image.currentSrc) && image.loaded
    ));
    if (allLoaded && signature === previousSignature) {
      stablePasses += 1;
      if (stablePasses >= 2) {
        removeRequestListeners();
        const images = await captureImages();
        return {
          images,
          complete: true,
          scrollAttempts,
          observationCoverage: coverage,
        };
      }
    } else {
      stablePasses = 0;
    }
    previousSignature = signature;
    await page.waitForTimeout(50);
  }

  removeRequestListeners();
  const images = await captureImages();
  return {
    images,
    complete: false,
    scrollAttempts,
    observationCoverage: coverage,
  };
}
