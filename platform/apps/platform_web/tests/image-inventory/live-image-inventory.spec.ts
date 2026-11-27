import { expect, test } from "@playwright/test";
import type { Page, Request, TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  collectLiveImageInventory,
  finalizeBrowserFailureEvidence,
  type BrowserFailureAttachmentState,
  type BrowserRequestFailureEvidence,
} from "../support/live-image-inventory";

const fixtureSvg = [
  '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">',
  '<rect width="32" height="32" fill="#d69a2d"/>',
  "</svg>",
].join("");

function producerImageEvidencePayload(
  route: "home" | "patch" | "tournaments",
  inventory: Awaited<ReturnType<typeof collectLiveImageInventory>>,
) {
  const images = inventory.images;
  const missingCurrentSrcCount = images.filter((image) => !image.currentSrc).length;
  const unloadedCount = images.filter((image) => !image.loaded).length;
  const countLimit = 512;
  const diagnosticLimit = 16;
  const boundedImages = images.filter((image) => image.diagnostic.ordinal <= countLimit);
  const selectedDiagnostics = [...boundedImages]
    .sort((left, right) => (
      Number(!right.currentSrc || !right.loaded) - Number(!left.currentSrc || !left.loaded)
      || left.diagnostic.ordinal - right.diagnostic.ordinal
    ))
    .slice(0, diagnosticLimit)
    .map(({ diagnostic }) => ({
      ordinal: diagnostic.ordinal,
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
  return {
    schema: 2,
    route,
    image_count: Math.min(images.length, countLimit),
    missing_current_src_count: Math.min(missingCurrentSrcCount, countLimit),
    unloaded_count: Math.min(unloadedCount, countLimit),
    complete: inventory.complete,
    counts_truncated: images.length > countLimit
      || missingCurrentSrcCount > countLimit
      || unloadedCount > countLimit,
    image_entries_truncated: images.length > diagnosticLimit
      || boundedImages.length !== images.length,
    coverage: {
      network_state: inventory.observationCoverage.networkState,
      csp_state: inventory.observationCoverage.cspState,
      unmatched_csp_count: inventory.observationCoverage.unmatchedCspCount,
      csp_events_truncated: inventory.observationCoverage.cspEventsTruncated,
    },
    images: selectedDiagnostics,
  };
}

function writePrivateProducerFixture(payload: object, name = "producer-image-inventory-v2.json") {
  return writePrivateProducerBytes(name, Buffer.from(JSON.stringify(payload), "ascii"));
}

function writePrivateProducerBytes(name: string, body: Buffer) {
  const buildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;
  if (!buildDir || !path.isAbsolute(buildDir)) {
    throw new Error("image inventory fixture output directory is unavailable");
  }
  const currentUid = process.getuid?.();
  if (currentUid === undefined) {
    throw new Error("image inventory fixture output owner cannot be verified");
  }
  const directory = fs.lstatSync(buildDir);
  if (!directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== currentUid) {
    throw new Error("image inventory fixture output directory identity is unsafe");
  }
  const output = path.join(buildDir, name);
  const fd = fs.openSync(
    output,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
      | (fs.constants.O_NOFOLLOW ?? 0),
    0o600,
  );
  try {
    const metadata = fs.fstatSync(fd);
    if (!metadata.isFile() || metadata.uid !== currentUid || (metadata.mode & 0o777) !== 0o600) {
      throw new Error("image inventory producer fixture identity is unsafe");
    }
    fs.writeFileSync(fd, body);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  return body.length;
}

function registerTenImageFixture(
  page: Page,
  { failedImage = false, failedImageMode = "abort" as "abort" | "http-error" } = {},
) {
  return page.route("http://image-inventory.test/**", async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === "/fixture") {
        await route.fulfill({
          contentType: "text/html; charset=utf-8",
          body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content" style="padding-top: 1800px"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((names) => {
                  const content = document.querySelector("#image-content");
                  for (const name of names) {
                    const image = document.createElement("img");
                    image.alt = name;
                    image.loading = "lazy";
                    image.width = 32;
                    image.height = 32;
                    image.src = "/images/" + name + ".svg";
                    content.append(image);
                  }
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                });
            </script>
          </body></html>`,
      });
      return;
    }
    if (pathname === "/api/content/initial") {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(Array.from({ length: 10 }, (_, index) => `asset-${index + 1}`)),
      });
      return;
    }
    if (failedImage && pathname === "/images/asset-10.svg") {
      if (failedImageMode === "http-error") {
        await route.fulfill({ status: 404, contentType: "text/plain", body: "missing" });
      } else {
        await route.abort("failed");
      }
      return;
    }
    if (/^\/images\/asset-(?:[1-9]|10)\.svg$/u.test(pathname)) {
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    if (pathname === "/api/late-failure") {
      await new Promise((resolve) => setTimeout(resolve, 120));
      await route.abort("failed");
      return;
    }
    await route.abort();
  });
}

function requestFailureClass(errorText: string): BrowserRequestFailureEvidence["events"][number]["failure_class"] {
  if (/ERR_NAME_NOT_RESOLVED|ERR_DNS/iu.test(errorText)) return "dns";
  if (/ERR_CONNECTION|ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_CHANGED/iu.test(errorText)) return "connection";
  if (/ERR_TIMED_OUT|ERR_CONNECTION_TIMEOUT/iu.test(errorText)) return "timeout";
  if (/ERR_CERT|ERR_SSL|ERR_TLS/iu.test(errorText)) return "tls";
  if (/ERR_ABORTED|cancelled/iu.test(errorText)) return "aborted";
  return "other";
}

function durationBucket(durationMs: number): BrowserRequestFailureEvidence["events"][number]["duration_bucket"] {
  if (durationMs < 100) return "<100ms";
  if (durationMs < 1_000) return "<1s";
  if (durationMs < 5_000) return "<5s";
  return ">=5s";
}

function browserFailureState(
  imageRoute: object,
  event: BrowserRequestFailureEvidence["events"][number] | null,
): BrowserFailureAttachmentState {
  return {
    imageInventoryRoutes: [imageRoute],
    attachedImageInventoryRoutes: new Set(),
    requestFailureEvidence: {
      schema: 1,
      coverage: "since_before_each",
      failure_phase: "unknown",
      truncated: false,
      events: event ? [event] : [],
    },
    requestFailureEvidenceAttached: false,
  };
}

async function collectTenImageInventory(page: Page, failedImage = false, failedImageMode?: "abort" | "http-error") {
  await registerTenImageFixture(page, { failedImage, failedImageMode });
  const failureEvent = failedImage && failedImageMode !== "http-error"
    ? observeRequestFailure(page, "/images/asset-10.svg")
    : null;
  const inventory = await collectLiveImageInventory(page, {
    route: "/",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
    },
    timeoutMs: 1_500,
  });
  return { inventory, requestFailure: failureEvent ? await failureEvent : null };
}

function registerPatchScaleImageFixture(
  page: Page,
  { failureOrdinal = 0, overflowOrdinal = 0, viewportGated = false } = {},
): Promise<number[]> {
  const imageCount = 128;
  const initialCount = 96;
  const lateCount = imageCount - initialCount;
  const requestStartOrdinals: number[] = [];
  page.on("request", (request) => {
    if (request.resourceType() !== "image") return;
    const match = /^\/images\/patch-image-([1-9][0-9]*)\.svg$/u.exec(new URL(request.url()).pathname);
    if (match) requestStartOrdinals.push(Number(match[1]));
  });
  const setup = async () => {
    await page.addInitScript(() => {
      const scrollCounts = new WeakMap<HTMLImageElement, number>();
      const sourceOpportunityScrollCounts = new Map<number, number>();
      const original = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function (this: Element, ...args: Parameters<typeof original>) {
        if (this instanceof HTMLImageElement) {
          scrollCounts.set(this, (scrollCounts.get(this) ?? 0) + 1);
        }
        return Reflect.apply(original, this, args);
      };
      (window as Window & {
        __patchScaleImageProbe?: {
          scrollCountFor: (ordinal: number) => number;
          sourceOpportunityScrollCountFor: (ordinal: number) => number | null;
          recordSourceOpportunity: (ordinal: number, scrollCount: number) => void;
        };
      }).__patchScaleImageProbe = {
        scrollCountFor: (ordinal) => {
          const image = [...document.images].find((entry) => (
            entry.dataset.probeImageOrdinal === String(ordinal)
          ));
          return image ? scrollCounts.get(image) ?? 0 : 0;
        },
        sourceOpportunityScrollCountFor: (ordinal) => sourceOpportunityScrollCounts.get(ordinal) ?? null,
        recordSourceOpportunity: (ordinal, scrollCount) => {
          sourceOpportunityScrollCounts.set(ordinal, scrollCount);
        },
      };
    });
    await page.route("http://image-inventory.test/**", async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === "/fixture") {
        await route.fulfill({
          contentType: "text/html; charset=utf-8",
          body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              const addImages = (start, count) => {
                const content = document.querySelector("#image-content");
                for (let index = start; index < start + count; index += 1) {
                  const section = document.createElement("section");
                  section.style.height = "480px";
                  section.style.display = "grid";
                  section.style.placeItems = "center";
                  const image = document.createElement("img");
                  image.alt = "patch-image-" + (index + 1);
                  image.dataset.probeImageOrdinal = String(index + 1);
                  image.loading = "lazy";
                  image.width = 32;
                  image.height = 32;
                  const imageUrl = "/images/patch-image-" + (index + 1) + ".svg"
                    + (index + 1 === ${overflowOrdinal} ? "?oversized=" + "x".repeat(2_100) : "");
                  if (${viewportGated}) {
                    image.dataset.pendingSrc = imageUrl;
                    imageSourceObserver.observe(image);
                  } else {
                    image.src = imageUrl;
                  }
                  section.append(image);
                  content.append(section);
                }
              };
              const imageSourceObserver = new IntersectionObserver((entries) => {
                for (const entry of entries) {
                  const image = entry.target;
                  if (entry.isIntersecting && !image.hasAttribute("src")) {
                    image.src = image.dataset.pendingSrc;
                    image.dataset.sourcePublished = "true";
                    const probe = window.__patchScaleImageProbe;
                    const ordinal = Number(image.dataset.probeImageOrdinal);
                    probe?.recordSourceOpportunity(ordinal, probe.scrollCountFor(ordinal));
                  }
                }
              }, { threshold: [0] });
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((count) => {
                  addImages(0, count);
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                  if (${lateCount} > 0) {
                    setTimeout(() => {
                      void fetch("/api/content/late")
                        .then((response) => response.json())
                        .then((late) => addImages(count, late));
                    }, 30);
                  }
                });
            </script>
          </body></html>`,
        });
        return;
      }
      if (pathname === "/api/content/initial") {
        await route.fulfill({
          contentType: "application/json",
          body: String(initialCount),
        });
        return;
      }
      if (pathname === "/api/content/late") {
        await route.fulfill({
          contentType: "application/json",
          body: String(lateCount),
        });
        return;
      }
      const match = /^\/images\/patch-image-([1-9][0-9]*)\.svg$/u.exec(pathname);
      if (match) {
        if (Number(match[1]) === failureOrdinal) {
          await route.abort("failed");
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, Number(match[1]) === overflowOrdinal ? 400 : 8));
        await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
        return;
      }
      await route.abort();
    });
  };
  return setup().then(() => requestStartOrdinals);
}

type HookFailureScenario = {
  state: BrowserFailureAttachmentState;
  attachedNames: string[];
  afterEachFails: boolean;
  bodyFailureState: "failed" | "passed" | "unknown";
  hookError: Error;
  fixtureKind: "hook" | "body" | "both" | null;
  repeatFinalizer: boolean;
  bodyAssertionObserved: boolean;
};

const hookFailureScenarios = new WeakMap<Page, HookFailureScenario>();

function testInfoAttachment(testInfo: TestInfo, scenario: HookFailureScenario) {
  return async (name: string, options: { body: Buffer; contentType: "application/json" }) => {
    scenario.attachedNames.push(name);
    await testInfo.attach(name, options);
    const filename = !scenario.fixtureKind
      ? null
      : name === "live-image-inventory-evidence-v2"
      ? `producer-image-inventory-${scenario.fixtureKind}-v2.json`
      : name === "live-browser-request-failures-v1"
        ? `producer-browser-request-failures-${scenario.fixtureKind}-v1.json`
        : null;
    if (filename) writePrivateProducerBytes(filename, options.body);
  };
}

function observeRequestFailure(page: Page, expectedPath: string) {
  const starts = new WeakMap<Request, { at: number; navigationEpoch: number }>();
  let navigationEpoch = 0;
  return new Promise<BrowserRequestFailureEvidence["events"][number]>((resolve) => {
    const onFrameNavigated = (frame: import("@playwright/test").Frame) => {
      if (frame === page.mainFrame()) navigationEpoch += 1;
    };
    const onRequest = (request: Request) => {
      if (new URL(request.url()).pathname === expectedPath) {
        starts.set(request, { at: performance.now(), navigationEpoch });
      }
    };
    const onRequestFailed = (request: Request) => {
      if (new URL(request.url()).pathname !== expectedPath) return;
      page.off("request", onRequest);
      page.off("requestfailed", onRequestFailed);
      page.off("framenavigated", onFrameNavigated);
      const resourceType = request.resourceType();
      const allowedResourceTypes = [
        "document", "image", "stylesheet", "script", "font", "media", "xhr", "fetch", "other",
      ] as const;
      const errorText = request.failure()?.errorText ?? "failed";
      const started = starts.get(request);
      resolve({
        ordinal: 1,
        resource_type: allowedResourceTypes.includes(resourceType as typeof allowedResourceTypes[number])
          ? resourceType as typeof allowedResourceTypes[number]
          : "unknown",
        route_scope: "home",
        failure_class: requestFailureClass(errorText),
        duration_bucket: started === undefined
          ? "unavailable"
          : durationBucket(Math.max(0, performance.now() - started.at)),
        navigation_relation: started === undefined
          ? "unknown"
          : started.navigationEpoch === navigationEpoch
            ? "same"
            : "changed",
      });
    };
    page.on("framenavigated", onFrameNavigated);
    page.on("request", onRequest);
    page.on("requestfailed", onRequestFailed);
  });
}

test.afterEach(async ({ page }, testInfo) => {
  const scenario = hookFailureScenarios.get(page);
  if (!scenario) return;
  const bodyStatusAtHookEntry = testInfo.status === "failed"
    ? "failed"
    : testInfo.status === "passed"
      ? "passed"
      : "unknown";
  let caught: unknown;
  let finalizerCalls = 0;
  const runFinalizer = async () => {
    finalizerCalls += 1;
    await finalizeBrowserFailureEvidence({
      attach: testInfoAttachment(testInfo, scenario),
      state: scenario.state,
      runAssertions: () => {
        if (scenario.afterEachFails && scenario.state.requestFailureEvidence.events.length > 0) {
          throw scenario.hookError;
        }
      },
      bodyFailureState: bodyStatusAtHookEntry,
    });
  };
  try {
    await runFinalizer();
  } catch (error) {
    caught = error;
  }
  let repeatedErrorPreserved = true;
  if (scenario.repeatFinalizer) {
    let repeatedCaught: unknown;
    try {
      await runFinalizer();
    } catch (error) {
      repeatedCaught = error;
    }
    repeatedErrorPreserved = repeatedCaught === scenario.hookError;
  }
  const routeAttachmentCount = scenario.attachedNames.filter(
    (name) => name === "live-image-inventory-evidence-v2",
  ).length;
  const requestAttachmentCount = scenario.attachedNames.filter(
    (name) => name === "live-browser-request-failures-v1",
  ).length;
  writePrivateProducerFixture({
    schema: 1,
    fixture: scenario.fixtureKind,
    body_failure_state: scenario.bodyFailureState,
    observed_test_status_at_hook_entry: bodyStatusAtHookEntry,
    body_assertion_observed: testInfo.errors.length > 0,
    after_each_failed: scenario.afterEachFails,
    failure_phase: scenario.state.requestFailureEvidence.failure_phase,
    route_attachment_count: routeAttachmentCount,
    request_attachment_count: requestAttachmentCount,
    hook_error_result: scenario.afterEachFails
      ? caught === scenario.hookError ? "preserved" : "mismatch"
      : caught === undefined ? "absent" : "unexpected",
    repeated_finalizer_calls: finalizerCalls,
    repeated_error_result: scenario.repeatFinalizer
      ? repeatedErrorPreserved ? "preserved" : "mismatch"
      : "not_repeated",
  }, `browser-failure-lifecycle-${scenario.fixtureKind}-v1.json`);
  expect(scenario.attachedNames).toEqual([
    "live-image-inventory-evidence-v2",
    "live-browser-request-failures-v1",
  ]);
  expect(finalizerCalls).toBe(scenario.repeatFinalizer ? 2 : 1);
  expect(repeatedErrorPreserved).toBe(true);
  if (scenario.afterEachFails) {
    expect(caught).toBe(scenario.hookError);
    throw scenario.hookError;
  }
  expect(caught).toBeUndefined();
});

test("WebKit inventories delayed offscreen lazy images after route content is ready", async ({ page }) => {
  await page.route("http://image-inventory.test/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/fixture") {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content" style="padding-top: 1800px"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              const addImages = (names) => {
                const content = document.querySelector("#image-content");
                for (const name of names) {
                  const image = document.createElement("img");
                  image.alt = name;
                  image.loading = "lazy";
                  image.width = 32;
                  image.height = 32;
                  image.src = "/images/" + name + ".svg";
                  content.append(image);
                }
              };
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((names) => {
                  addImages(names);
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                  setTimeout(() => {
                    void fetch("/api/content/late")
                      .then((response) => response.json())
                      .then(addImages);
                  }, 30);
                });
            </script>
          </body></html>`,
      });
      return;
    }
    if (pathname === "/api/content/initial") {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({ contentType: "application/json", body: '["image-one", "image-two"]' });
      return;
    }
    if (pathname === "/api/content/late") {
      await new Promise((resolve) => setTimeout(resolve, 140));
      await route.fulfill({ contentType: "application/json", body: '["image-three"]' });
      return;
    }
    if (/^\/images\/image-(?:one|two|three)\.svg$/u.test(pathname)) {
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    await route.abort();
  });

  await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
  const inventory = await collectLiveImageInventory(page, {
    route: "delayed-image-fixture",
    waitForContent: async () => {
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
    },
  });

  expect(inventory.complete).toBe(true);
  expect(inventory.images.map((image) => image.source)).toEqual([
    "/images/image-one.svg",
    "/images/image-two.svg",
    "/images/image-three.svg",
  ]);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(inventory.scrollAttempts).toBeLessThanOrEqual(3);
});

// This controlled viewport-gated source publisher checks scan robustness; it is
// not a reproduction of WebKit's production lazy-loader implementation.
test("WebKit revisits lazy images while their viewport-gated source is still pending", async ({ page }) => {
  await page.addInitScript(() => {
    const imageIds = new WeakMap<HTMLImageElement, string>();
    const states = new WeakMap<HTMLImageElement, boolean>();
    const events: Array<{ id: string; state: "initial_intersecting" | "initial_outside" | "enter" | "leave" }> = [];
    const observe = (image: HTMLImageElement) => {
      const id = image.dataset.imageId;
      if (id) imageIds.set(image, id);
      observer.observe(image);
    };
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const image = entry.target as HTMLImageElement;
        const id = imageIds.get(image);
        if (!id) continue;
        const intersecting = entry.isIntersecting && entry.intersectionRatio > 0;
        const previous = states.get(image);
        const state = previous === undefined
          ? intersecting ? "initial_intersecting" : "initial_outside"
          : previous === intersecting ? null : intersecting ? "enter" : "leave";
        states.set(image, intersecting);
        if (state) events.push({ id, state });
      }
    }, { threshold: [0] });
    const attach = (node: Node) => {
      if (node instanceof HTMLImageElement) observe(node);
      node.childNodes.forEach(attach);
    };
    new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach(attach)))
      .observe(document, { childList: true, subtree: true });
    Object.defineProperty(window, "__imageInventoryVisibility", {
      value: { events },
      configurable: false,
    });
  });
  await page.route("http://image-inventory.test/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/fixture") {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content" style="padding-top: 1800px"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              const pendingLoads = new WeakMap();
              const lazySourceObserver = new IntersectionObserver((entries) => {
                for (const entry of entries) {
                  const image = entry.target;
                  if (entry.isIntersecting && !image.hasAttribute("src")) {
                    const timer = setTimeout(() => {
                      if (image.isConnected && !image.hasAttribute("src")) {
                        image.src = image.dataset.pendingSrc;
                      }
                    }, 120);
                    pendingLoads.set(image, timer);
                  } else if (!entry.isIntersecting) {
                    clearTimeout(pendingLoads.get(image));
                  }
                }
              }, { threshold: [0] });
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((names) => {
                  const content = document.querySelector("#image-content");
                  for (const name of names) {
                    const section = document.createElement("section");
                    section.style.height = "1200px";
                    section.style.display = "grid";
                    section.style.placeItems = "center";
                    const image = document.createElement("img");
                    image.dataset.imageId = name;
                    image.dataset.pendingSrc = "/images/" + name + ".svg";
                    image.loading = "lazy";
                    image.width = 32;
                    image.height = 32;
                    section.append(image);
                    lazySourceObserver.observe(image);
                    content.append(section);
                  }
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                });
            </script>
          </body></html>`,
      });
      return;
    }
    if (pathname === "/api/content/initial") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(Array.from({ length: 10 }, (_, index) => `lazy-${index + 1}`)),
      });
      return;
    }
    if (/^\/images\/lazy-(?:[1-9]|10)\.svg$/u.test(pathname)) {
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    await route.abort();
  });

  await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
  const inventory = await collectLiveImageInventory(page, {
    route: "viewport-gated-lazy-image-fixture",
    waitForContent: async () => {
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
    },
    timeoutMs: 10_000,
  });

  expect(inventory.complete).toBe(true);
  expect(inventory.images).toHaveLength(10);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(inventory.images.every((image) => (
    image.diagnostic.requestState === "request_finished"
    || image.diagnostic.requestState === "response_received"
  ))).toBe(true);
  const visibilityEvents = await page.evaluate(() => (
    (window as Window & {
      __imageInventoryVisibility?: { events: Array<{ id: string; state: string }> };
    }).__imageInventoryVisibility?.events ?? []
  ));
  for (const image of inventory.images) {
    const entered = visibilityEvents.some((event) => (
      event.id === image.source.split("/").at(-1)?.replace(/\.svg$/u, "")
      && (event.state === "initial_intersecting" || event.state === "enter")
    ));
    expect(entered, `lazy image ${image.diagnostic.ordinal} should actually enter the viewport`).toBe(true);
  }
});

test("WebKit completes a patch-scale inventory after dynamically added lazy images start", async ({ page }) => {
  await registerPatchScaleImageFixture(page);
  const inventory = await collectLiveImageInventory(page, {
    route: "patch-scale-lazy-images",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
      await expect.poll(() => page.locator("img").count()).toBe(128);
    },
  });

  expect(inventory.images).toHaveLength(128);
  expect(inventory.complete).toBe(true);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(inventory.scrollAttempts).toBeLessThanOrEqual(128);
  expect(inventory.observationCoverage.networkState).toBe("truncated");
});

test("WebKit waits for real viewport delivery before assigning patch-scale lazy sources", async ({ page }) => {
  const requestStartOrdinals = await registerPatchScaleImageFixture(page, { viewportGated: true });
  const inventory = await collectLiveImageInventory(page, {
    route: "patch-scale-intersection-lazy-images",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
      await expect.poll(() => page.locator("img").count()).toBe(128);
    },
  });
  const publishedCount = await page.locator("img").evaluateAll((elements) => (
    (elements as HTMLImageElement[]).filter((image) => image.dataset.sourcePublished === "true").length
  ));

  expect(inventory.images).toHaveLength(128);
  expect(inventory.complete).toBe(true);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(publishedCount).toBe(128);
  expect(requestStartOrdinals.length).toBeGreaterThan(64);
  expect(inventory.observationCoverage.networkState).toBe("truncated");
});

test("WebKit records a new viewport opportunity when currentSrc changes on the same node", async ({ page }) => {
  let releaseSourceSwitchScroll: () => void = () => undefined;
  let releaseNewSourceResponse: () => void = () => undefined;
  let releaseBlockerResponse: () => void = () => undefined;
  const sourceSwitchScroll = new Promise<void>((resolve) => {
    releaseSourceSwitchScroll = resolve;
  });
  const newSourceResponse = new Promise<void>((resolve) => {
    releaseNewSourceResponse = resolve;
  });
  const blockerResponse = new Promise<void>((resolve) => {
    releaseBlockerResponse = resolve;
  });
  let sourceSwitchPromise: Promise<void> = Promise.resolve();
  let initialSourceAttributes: { src: string | null; srcset: string | null } | null = null;

  await page.addInitScript(() => {
    const scrollSources: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element, ...args: Parameters<typeof original>) {
      if (this instanceof HTMLImageElement && this.id === "responsive-patch-image") {
        scrollSources.push(this.currentSrc.split("/").at(-1) ?? "unknown");
        (window as Window & { __responsivePatchProbe?: { scrollSources: string[] } })
          .__responsivePatchProbe = { scrollSources };
      }
      return Reflect.apply(original, this, args);
    };
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const image = entry.target;
        if (entry.isIntersecting && image instanceof HTMLImageElement && image.id === "responsive-patch-image") {
          const name = image.currentSrc.split("/").at(-1);
          if (name === "responsive-light.svg") {
            (window as Window & { __responsivePatchProbe?: { firstSourceVisible: boolean } })
              .__responsivePatchProbe = {
                ...(window as Window & { __responsivePatchProbe?: { scrollSources: string[] } })
                  .__responsivePatchProbe,
                firstSourceVisible: true,
              };
          }
        }
      }
    }, { threshold: [0] });
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node instanceof HTMLImageElement && node.id === "responsive-patch-image") observer.observe(node);
          if (node instanceof Element) {
            const image = node.querySelector<HTMLImageElement>("#responsive-patch-image");
            if (image) observer.observe(image);
          }
        }
      }
    }).observe(document, { subtree: true, childList: true });
  });
  await page.route("http://image-inventory.test/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/fixture") {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body><main style="padding-top: 2000px"><picture>
            <source media="(prefers-color-scheme: light)" srcset="/images/responsive-light.svg">
            <source media="(prefers-color-scheme: dark)" srcset="/images/responsive-dark.svg">
            <img id="responsive-patch-image" alt="responsive-patch" width="32" height="32"
              loading="eager" src="/images/responsive-fallback.svg">
            </picture>
            <img id="inventory-blocker" alt="inventory-blocker" width="32" height="32"
              loading="eager" src="/images/inventory-blocker.svg"></main></body></html>`,
      });
      return;
    }
    if (pathname === "/images/responsive-light.svg") {
      await sourceSwitchScroll;
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    if (pathname === "/images/responsive-dark.svg") {
      await newSourceResponse;
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    if (pathname === "/images/inventory-blocker.svg") {
      await blockerResponse;
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    await route.abort();
  });

  const inventoryPromise = collectLiveImageInventory(page, {
    route: "responsive-current-src-generation",
    waitForContent: async () => {
      await page.emulateMedia({ colorScheme: "light" });
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect.poll(() => page.locator("#responsive-patch-image").evaluate((image) => (
        (image as HTMLImageElement).currentSrc.split("/").at(-1)
      ))).toBe("responsive-light.svg");
      initialSourceAttributes = await page.locator("#responsive-patch-image").evaluate((image) => ({
        src: image.getAttribute("src"),
        srcset: image.getAttribute("srcset"),
      }));
      sourceSwitchPromise = (async () => {
        await expect.poll(() => page.evaluate(() => (
          (window as Window & { __responsivePatchProbe?: { firstSourceVisible?: boolean } })
            .__responsivePatchProbe?.firstSourceVisible === true
        ))).toBe(true);
        releaseSourceSwitchScroll();
        await expect.poll(() => page.locator("#responsive-patch-image").evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
        await page.emulateMedia({ colorScheme: "dark" });
        await expect.poll(() => page.locator("#responsive-patch-image").evaluate((image) => (
          (image as HTMLImageElement).currentSrc.split("/").at(-1)
        ))).toBe("responsive-dark.svg");
        await expect.poll(() => page.evaluate(() => (
          (window as Window & { __responsivePatchProbe?: { scrollSources: string[] } })
            .__responsivePatchProbe?.scrollSources.includes("responsive-dark.svg") === true
        ))).toBe(true);
        releaseNewSourceResponse();
        releaseBlockerResponse();
        await expect.poll(() => page.locator("#responsive-patch-image").evaluate((image) => (
          (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
        ))).toBe(true);
      })();
    },
  });
  const inventory = await inventoryPromise;
  await sourceSwitchPromise;
  const observedScrollSources = await page.evaluate(() => (
    (window as Window & { __responsivePatchProbe?: { scrollSources: string[] } })
      .__responsivePatchProbe?.scrollSources ?? []
  ));
  const finalSourceAttributes = await page.locator("#responsive-patch-image").evaluate((image) => ({
    src: image.getAttribute("src"),
    srcset: image.getAttribute("srcset"),
  }));

  expect(inventory.images).toHaveLength(2);
  expect(inventory.complete).toBe(true);
  expect(inventory.images[0].loaded).toBe(true);
  expect(inventory.images[0].currentSrc.split("/").at(-1)).toBe("responsive-dark.svg");
  expect(finalSourceAttributes).toEqual(initialSourceAttributes);
  expect(observedScrollSources).toEqual(expect.arrayContaining([
    "responsive-light.svg",
    "responsive-dark.svg",
  ]));
});

test("WebKit keeps a failed tail image after request detail coverage truncates", async ({ page }) => {
  const failedOrdinal = 128;
  const requestStartOrdinals = await registerPatchScaleImageFixture(page, { failureOrdinal: failedOrdinal });
  const inventory = await collectLiveImageInventory(page, {
    route: "patch-scale-failed-tail-image",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
      await expect(page.locator("img")).toHaveCount(128);
    },
    timeoutMs: 12_000,
  });

  const evidence = producerImageEvidencePayload("patch", inventory);
  const failed = evidence.images.find((image) => image.ordinal === failedOrdinal);
  const failureRequestStartPosition = requestStartOrdinals.indexOf(failedOrdinal) + 1;
  const failedImageScrollCount = await page.evaluate((ordinal) => (
    (window as Window & {
      __patchScaleImageProbe?: { scrollCountFor: (value: number) => number };
    }).__patchScaleImageProbe?.scrollCountFor(ordinal) ?? 0
  ), failedOrdinal);
  expect(inventory.images).toHaveLength(128);
  expect(inventory.complete).toBe(false);
  expect(evidence.unloaded_count).toBe(1);
  expect(evidence.coverage.network_state).toBe("truncated");
  expect(evidence.images).toHaveLength(16);
  expect(failed).toMatchObject({
    complete: true,
    current_src_present: true,
    natural_width_positive: false,
    request_failed: null,
    request_failure_class: "unavailable",
    request_state: "unavailable",
  });
  expect(failureRequestStartPosition).toBeGreaterThan(64);
  expect(failedImageScrollCount).toBeLessThanOrEqual(1);
  expect(inventory.scrollAttempts).toBeLessThanOrEqual(128);
});

test("WebKit avoids repeat scrolling after opportunity when request-start membership overflows", async ({ page }) => {
  const overflowOrdinal = 128;
  const requestStartOrdinals = await registerPatchScaleImageFixture(page, {
    overflowOrdinal,
    viewportGated: true,
  });
  const inventory = await collectLiveImageInventory(page, {
    route: "patch-scale-request-start-overflow",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
      await expect(page.locator("img")).toHaveCount(128);
    },
  });
  const overflowImageScrollCount = await page.evaluate((ordinal) => (
    (window as Window & {
      __patchScaleImageProbe?: {
        scrollCountFor: (value: number) => number;
        sourceOpportunityScrollCountFor: (value: number) => number | null;
      };
    }).__patchScaleImageProbe?.scrollCountFor(ordinal) ?? 0
  ), overflowOrdinal);
  const overflowImageScrollCountAtOpportunity = await page.evaluate((ordinal) => (
    (window as Window & {
      __patchScaleImageProbe?: { sourceOpportunityScrollCountFor: (value: number) => number | null };
    }).__patchScaleImageProbe?.sourceOpportunityScrollCountFor(ordinal) ?? null
  ), overflowOrdinal);

  expect(inventory.images).toHaveLength(128);
  expect(inventory.complete).toBe(true);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);
  expect(requestStartOrdinals.indexOf(overflowOrdinal) + 1).toBeGreaterThan(64);
  expect(overflowImageScrollCountAtOpportunity).not.toBeNull();
  expect(overflowImageScrollCount).toBe(overflowImageScrollCountAtOpportunity);
  expect(inventory.observationCoverage.networkState).toBe("truncated");
});

test("WebKit emits bounded producer evidence for ten images with one failed asset", async ({ page }) => {
  await page.route("http://image-inventory.test/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname === "/fixture") {
      await route.fulfill({
        contentType: "text/html; charset=utf-8",
        body: `<!doctype html>
          <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
          <body>
            <main id="image-content" style="padding-top: 1800px"></main>
            <div data-testid="route-ready" data-ready="false"></div>
            <script>
              fetch("/api/content/initial")
                .then((response) => response.json())
                .then((names) => {
                  const content = document.querySelector("#image-content");
                  for (const name of names) {
                    const image = document.createElement("img");
                    image.alt = name;
                    image.loading = "lazy";
                    image.width = 32;
                    image.height = 32;
                    image.src = "/images/" + name + ".svg";
                    content.append(image);
                  }
                  document.querySelector('[data-testid="route-ready"]')
                    .setAttribute("data-ready", "true");
                });
            </script>
          </body></html>`,
      });
      return;
    }
    if (pathname === "/api/content/initial") {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(Array.from({ length: 10 }, (_, index) => `asset-${index + 1}`)),
      });
      return;
    }
    if (pathname === "/images/asset-10.svg") {
      await route.abort("failed");
      return;
    }
    if (/^\/images\/asset-[1-9]\.svg$/u.test(pathname)) {
      await route.fulfill({ contentType: "image/svg+xml", body: fixtureSvg });
      return;
    }
    await route.abort();
  });

  const inventory = await collectLiveImageInventory(page, {
    route: "/",
    waitForContent: async () => {
      await page.goto("http://image-inventory.test/fixture", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("route-ready")).toHaveAttribute("data-ready", "true");
    },
    timeoutMs: 1_500,
  });

  expect(inventory.images).toHaveLength(10);
  expect(inventory.images.filter((image) => !image.loaded)).toHaveLength(1);
  const failed = inventory.images.find((image) => !image.loaded);
  expect(failed?.currentSrc.length).toBeGreaterThan(0);
  expect(failed?.diagnostic.complete).toBe(true);
  expect(failed?.diagnostic.naturalWidthPositive).toBe(false);
  expect(failed?.diagnostic.requestState).toBe("request_failed");
  expect(failed?.diagnostic.responseReceived).toBe(false);
  expect(failed?.diagnostic.responseStatusClass).toBe("none");
  expect(failed?.diagnostic.requestFailed).toBe(true);

  const payload = producerImageEvidencePayload("home", inventory);
  expect(payload.image_count).toBe(10);
  expect(payload.unloaded_count).toBe(1);
  expect(payload.missing_current_src_count).toBe(0);
  expect(payload.complete).toBe(false);
  expect(payload.images).toHaveLength(10);
  const payloadBytes = writePrivateProducerFixture(payload);
  expect(payloadBytes).toBeLessThan(8 * 1024);
});

test.fail("WebKit attaches image evidence when only the request-failure hook fails", async ({ page }) => {
  const { inventory } = await collectTenImageInventory(page);
  expect(inventory.complete).toBe(true);
  expect(inventory.images).toHaveLength(10);
  expect(inventory.images.every((image) => image.loaded && image.currentSrc.length > 0)).toBe(true);

  const lateFailure = observeRequestFailure(page, "/api/late-failure");
  await page.evaluate(async () => fetch("/api/late-failure").catch(() => undefined));
  const event = await lateFailure;
  expect(event.resource_type).toBe("fetch");
  expect(event.route_scope).toBe("home");
  expect(event.failure_class).toBe("other");

  hookFailureScenarios.set(page, {
    state: browserFailureState(producerImageEvidencePayload("home", inventory), event),
    attachedNames: [],
    afterEachFails: true,
    bodyFailureState: "passed",
    hookError: new Error("closed afterEach request-failure assertion"),
    fixtureKind: "hook",
    repeatFinalizer: false,
    bodyAssertionObserved: false,
  });
});

test.fail("WebKit preserves body failure evidence when the afterEach assertion passes", async ({ page }) => {
  const { inventory } = await collectTenImageInventory(page, true, "http-error");
  expect(inventory.images).toHaveLength(10);
  expect(inventory.images.filter((image) => !image.loaded)).toHaveLength(1);
  hookFailureScenarios.set(page, {
    state: browserFailureState(producerImageEvidencePayload("home", inventory), null),
    attachedNames: [],
    afterEachFails: false,
    bodyFailureState: "failed",
    hookError: new Error("unused afterEach failure"),
    fixtureKind: "body",
    repeatFinalizer: false,
    bodyAssertionObserved: true,
  });
  expect(inventory.complete).toBe(true);
});

test.fail("WebKit attaches body and hook evidence only once when both fail", async ({ page }) => {
  const { inventory, requestFailure } = await collectTenImageInventory(page, true);
  expect(inventory.images).toHaveLength(10);
  expect(inventory.images.filter((image) => !image.loaded)).toHaveLength(1);
  expect(requestFailure?.resource_type).toBe("image");

  hookFailureScenarios.set(page, {
    state: browserFailureState(producerImageEvidencePayload("home", inventory), requestFailure),
    attachedNames: [],
    afterEachFails: true,
    bodyFailureState: "failed",
    hookError: new Error("closed afterEach request-failure assertion"),
    fixtureKind: "both",
    repeatFinalizer: true,
    bodyAssertionObserved: true,
  });
  expect(inventory.complete).toBe(true);
});

test("WebKit lifecycle attachments survive independent verification", async () => {
  const buildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;
  if (!buildDir || !path.isAbsolute(buildDir)) {
    throw new Error("image inventory fixture output directory is unavailable");
  }
  const readClosedFixture = (name: string) => {
    const filename = path.join(buildDir, name);
    const metadata = fs.lstatSync(filename);
    expect(metadata.isFile() && !metadata.isSymbolicLink()).toBe(true);
    const currentUid = process.getuid?.();
    expect(currentUid).not.toBeUndefined();
    expect(metadata.uid).toBe(currentUid);
    expect(metadata.mode & 0o777).toBe(0o600);
    return JSON.parse(fs.readFileSync(filename, "ascii"));
  };

  for (const [fixture, expectedPhase, expectedBodyState, expectedLoadedCount] of [
    ["hook", "hook", "passed", 10],
    ["body", "body", "failed", 9],
    ["both", "both", "failed", 9],
  ] as const) {
    const inventory = readClosedFixture(`producer-image-inventory-${fixture}-v2.json`);
    const requests = readClosedFixture(`producer-browser-request-failures-${fixture}-v1.json`);
    const ledger = readClosedFixture(`browser-failure-lifecycle-${fixture}-v1.json`);
    expect(inventory.schema).toBe(2);
    expect(inventory.route).toBe("home");
    expect(inventory.image_count).toBe(10);
    expect(inventory.images.filter((image: { complete: boolean; natural_width_positive: boolean }) => (
      image.complete && image.natural_width_positive
    ))).toHaveLength(expectedLoadedCount);
    expect(inventory.complete).toBe(fixture === "hook");
    expect(inventory.unloaded_count).toBe(fixture === "hook" ? 0 : 1);
    expect(requests.schema).toBe(1);
    expect(requests.failure_phase).toBe(expectedPhase);
    expect(ledger).toEqual({
      schema: 1,
      fixture,
      body_failure_state: expectedBodyState,
      observed_test_status_at_hook_entry: fixture === "hook" ? "passed" : "failed",
      body_assertion_observed: fixture !== "hook",
      after_each_failed: fixture !== "body",
      failure_phase: expectedPhase,
      route_attachment_count: 1,
      request_attachment_count: 1,
      hook_error_result: fixture === "body" ? "absent" : "preserved",
      repeated_finalizer_calls: fixture === "both" ? 2 : 1,
      repeated_error_result: fixture === "both" ? "preserved" : "not_repeated",
    });
    expect(Object.keys(ledger).some((key) => /url|host|source|error_text|dom/iu.test(key))).toBe(false);
  }

  const hookRequests = readClosedFixture("producer-browser-request-failures-hook-v1.json");
  expect(hookRequests.events).toHaveLength(1);
  expect(hookRequests.events[0]).toEqual({
    ordinal: 1,
    resource_type: "fetch",
    route_scope: "home",
    failure_class: "other",
    duration_bucket: "<1s",
    navigation_relation: "same",
  });
  const bodyRequests = readClosedFixture("producer-browser-request-failures-body-v1.json");
  expect(bodyRequests.events).toEqual([]);
  const bothRequests = readClosedFixture("producer-browser-request-failures-both-v1.json");
  expect(bothRequests.events).toHaveLength(1);
  expect(bothRequests.events[0].resource_type).toBe("image");
});

test("browser request-failure evidence stays below its 4 KiB cap", async () => {
  const attachments: Array<{ name: string; body: Buffer }> = [];
  const state: BrowserFailureAttachmentState = {
    imageInventoryRoutes: [],
    attachedImageInventoryRoutes: new Set(),
    requestFailureEvidence: {
      schema: 1,
      coverage: "since_before_each",
      failure_phase: "unknown",
      truncated: false,
      events: Array.from({ length: 64 }, (_, index) => ({
        ordinal: index + 1,
        resource_type: "stylesheet",
        route_scope: "tournaments",
        failure_class: "connection",
        duration_bucket: ">=5s",
        navigation_relation: "same",
      })),
    },
    requestFailureEvidenceAttached: false,
  };
  const originalError = new Error("synthetic bounded request failure");
  let caught: unknown;
  try {
    await finalizeBrowserFailureEvidence({
      attach: async (name, { body }) => {
        attachments.push({ name, body });
      },
      state,
      runAssertions: () => { throw originalError; },
      bodyFailureState: "passed",
    });
  } catch (error) {
    caught = error;
  }

  expect(caught).toBe(originalError);
  expect(attachments).toHaveLength(1);
  expect(attachments[0].name).toBe("live-browser-request-failures-v1");
  expect(attachments[0].body.length).toBeLessThanOrEqual(4 * 1024);
  const evidence = JSON.parse(
    attachments[0].body.toString("ascii"),
  ) as BrowserRequestFailureEvidence;
  expect(evidence.failure_phase).toBe("hook");
  expect(evidence.truncated).toBe(true);
  expect(evidence.events.length).toBeLessThan(64);
  expect(evidence.events.map((event) => event.ordinal)).toEqual(
    Array.from({ length: evidence.events.length }, (_, index) => index + 1),
  );
  expect(attachments[0].body.includes(Buffer.from("http"))).toBe(false);
});
