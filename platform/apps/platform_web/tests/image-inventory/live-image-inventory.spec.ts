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
  const directory = fs.lstatSync(buildDir);
  if (!directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== process.getuid()) {
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
    if (!metadata.isFile() || metadata.uid !== process.getuid() || (metadata.mode & 0o777) !== 0o600) {
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
  expect(inventory.scrollAttempts).toBe(3);
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
    expect(metadata.uid).toBe(process.getuid());
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
      attach: async (name, { body }) => attachments.push({ name, body }),
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
