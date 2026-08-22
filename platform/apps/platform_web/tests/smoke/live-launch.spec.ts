import { expect, test } from "@playwright/test";
import { assertLiveQaChromiumSandbox } from "../support/live-qa-sandbox";

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
  requestFailures: string[];
  unexpectedAuthAutomationRequests: string[];
};

const browserEvidence = new WeakMap<import("@playwright/test").Page, BrowserEvidence>();
const reportOnlyFrameAncestorsWarning =
  "The Content Security Policy directive 'frame-ancestors' is ignored when delivered in a report-only policy.";

test.beforeEach(async ({ page }) => {
  const evidence: BrowserEvidence = {
    consoleCspErrors: [],
    cspViolations: [],
    pageErrors: [],
    requestFailures: [],
    unexpectedAuthAutomationRequests: [],
  };
  browserEvidence.set(page, evidence);
  page.on("request", (request) => {
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
  page.on("pageerror", (error) => {
    evidence.pageErrors.push(error.name || "page error");
  });
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
    const isExpectedNavigationApiCancellation = (
      errorText === "Load request cancelled"
      && request.method() === "GET"
      && parsed.origin === new URL(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").origin
      && [
        "/api/v1/content/support/status",
        "/api/v1/users/me",
      ].includes(parsed.pathname)
    );
    if (
      isExpectedNextPrefetchAbort
      || isExpectedNavigationApiCancellation
    ) {
      return;
    }
    evidence.requestFailures.push(
      `${request.method()} ${parsed.origin}${parsed.pathname} ${errorText}`,
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
        disposition: string;
        effectiveDirective: string;
        documentURI: string;
        sourceFile: string;
        lineNumber: number;
        columnNumber: number;
      }>;
    };
    target.__platformCspViolations = [];
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
      target.__platformCspViolations?.push(violation);
      void target.__platformRecordCspViolation?.(violation);
    });
  });
});

test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.annotations.some((annotation) => annotation.type === "expected-csp-violations")) {
    return;
  }
  const evidence = browserEvidence.get(page);
  expect(evidence?.cspViolations ?? [], "SecurityPolicyViolationEvent entries").toEqual([]);
  expect(evidence?.consoleCspErrors ?? [], "CSP console errors").toEqual([]);
  expect(evidence?.pageErrors ?? [], "uncaught page errors").toEqual([]);
  expect(evidence?.requestFailures ?? [], "failed browser requests").toEqual([]);
  expect(
    evidence?.unexpectedAuthAutomationRequests ?? [],
    "auth documents and Turnstile belong to the separate human gate",
  ).toEqual([]);
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

test("production Chromium live QA keeps its process sandbox enabled", async ({ page, browserName }) => {
  test.skip(
    !/^https:\/\/old-sparky\.com\/?$/u.test(
      process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "",
    ),
    "Production-only process sandbox assertion.",
  );
  test.skip(browserName !== "chromium", "Chromium-only process sandbox assertion.");
  await assertLiveQaChromiumSandbox(page.context(), browserName);
});

test("live CSP nonce is stable on soft navigation and rotates on hard reload", async ({ page }) => {
  const initialResponse = await page.goto("/");
  expect(initialResponse).not.toBeNull();
  const initialNonce = responseCspNonce(initialResponse!);
  await expectDocumentNonce(page, initialNonce);

  await page.locator('a[href="/info"]').first().click();
  await expect(page).toHaveURL(/\/info$/u);
  await expectDocumentNonce(page, initialNonce);

  const reloadedResponse = await page.reload();
  expect(reloadedResponse).not.toBeNull();
  const reloadedNonce = responseCspNonce(reloadedResponse!);
  expect(reloadedNonce).not.toBe(initialNonce);
  await expectDocumentNonce(page, reloadedNonce);
});

test("local enforced CSP blocks negative inline and external probes", async ({ page }, testInfo) => {
  test.skip(
    process.env.PLATFORM_LOCAL_CSP_NEGATIVE_QA !== "1",
    "PLATFORM_LOCAL_CSP_NEGATIVE_QA=1 is required.",
  );
  test.skip(testInfo.project.name !== "live-desktop", "One Chromium enforcement probe is sufficient.");
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
    styledPosition: "static",
  });
  expect(forbiddenRequestCount).toBe(0);
  await expect.poll(async () => page.evaluate(() => (
    (globalThis as typeof globalThis & {
      __platformCspViolations?: Array<{ effectiveDirective: string }>;
    }).__platformCspViolations?.map((value) => value.effectiveDirective) ?? []
  ))).toEqual(expect.arrayContaining([
    "script-src-elem",
    "script-src-attr",
    "style-src-attr",
  ]));
});

test("live tournaments hub exposes a valid empty or populated list", async ({ page }) => {
  await page.goto("/tournaments");

  const cards = page.getByTestId("tournament-card");
  const emptyState = page.locator(".tournament-list-state");
  await expect(cards.first().or(emptyState)).toBeVisible();
  const cardCount = await cards.count();
  if (cardCount === 0) {
    await expect(emptyState).toContainText("Турниров пока нет");
    await expect(page.getByText("Показано 0 из 0 турниров")).toBeVisible();
    return;
  }

  await waitForTournamentCardAssets(page);
  await expect(page.getByLabel("Выбор языка")).toHaveCount(0);
  await expect(cards.first().getByRole("heading")).toBeVisible();

  await expect(cards.first()).toHaveAttribute("href", /^\/tournaments\/[a-z0-9-]+$/i);
});

test("live 1920 catalog stays contained after every card asset loads", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "live-desktop", "The visual artifact has one canonical desktop viewport.");
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/tournaments");

  const cards = page.getByTestId("tournament-card");
  await expect(cards.first().or(page.locator(".tournament-list-state"))).toBeVisible();
  const cardCount = await cards.count();
  test.skip(cardCount === 0, "The production tournament list is intentionally empty.");
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
  const sharedCoverBackgrounds = await cards.evaluateAll((items) => items
    .map((item) => getComputedStyle(item.querySelector<HTMLElement>(".card-banner")!).backgroundImage)
    .filter((background) => background.includes("/assets/tournament-covers/")));
  expect(sharedCoverBackgrounds.length).toBeGreaterThan(0);
  expect(sharedCoverBackgrounds.every((background) => background.includes("rev=20260725-2"))).toBe(true);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    animations: "allow",
    caret: "initial",
    fullPage: true,
    path: testInfo.outputPath("production-tournaments-1920-loaded.png")
  });
});

test("live tournament detail and bracket routes render from the current public data", async ({ page }) => {
  await page.goto("/tournaments");
  const firstDetailsLink = page.getByTestId("tournament-card").first();
  await expect(firstDetailsLink.or(page.locator(".tournament-list-state"))).toBeVisible();
  test.skip((await firstDetailsLink.count()) === 0, "The production tournament list is intentionally empty.");
  const href = await firstDetailsLink.getAttribute("href");
  expect(href).toBeTruthy();

  await page.goto(href!);
  await expect(page.getByText("Описание турнира")).toBeVisible();
  await expect(page.getByRole("link", { name: "Перейти к сетке" })).toHaveAttribute("href", `${href}/bracket`);
  await expectNoHorizontalOverflow(page);

  await page.goto(`${href}/bracket`);
  await expect(
    page.getByTestId("bracket-shell").or(page.getByTestId("bracket-empty"))
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("live admin route is protected for anonymous users", async ({ page }) => {
  await page.goto("/platform-ops");
  expect(new URL(page.url()).hostname).toMatch(/\.cloudflareaccess\.com$/u);
  await expect(page.getByTestId("admin-console")).toHaveCount(0);
  await page.waitForLoadState("networkidle");

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

test("live patch renders separate Urn and Rift objectives with source icons", async ({ page }, testInfo) => {
  await page.goto("/patches/1836506165584438");

  const objectives = page.locator(".objective-patch-section");
  await expect(objectives).toHaveCount(2);
  await expect(objectives.getByRole("heading", { level: 2 })).toHaveText([
    "Урна",
    "Нестабильный разлом"
  ]);
  const sourceIcons = objectives.locator('.patch-objective-image[data-objective-icon="source"]');
  await expect(sourceIcons).toHaveCount(2);
  await expect(sourceIcons.first()).toHaveAttribute(
    "src",
    /^https:\/\/assets-bucket\.deadlock-api\.com\//u
  );
  await expect.poll(async () => sourceIcons.evaluateAll((images) => images.every(
    (image) => (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0
  ))).toBe(true);
  await expectNoHorizontalOverflow(page);
  if (testInfo.project.name !== "live-webkit-mobile") {
    await page.screenshot({
      animations: "allow",
      caret: "initial",
      fullPage: true,
      path: testInfo.outputPath("production-patch-objectives.png")
    });
  }
});

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

test("live image currentSrc inventory stays inside the exact CSP hosts", async ({ page }, testInfo) => {
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
  for (const route of ["/", "/patches/1836506165584438", "/tournaments"]) {
    await page.goto(route);
    allowedHosts.add(new URL(page.url()).hostname);
    const imageLocator = page.locator("img");
    let previousCount = -1;
    let stablePasses = 0;
    for (let pass = 0; pass < 5 && stablePasses < 2; pass += 1) {
      const imageCount = await imageLocator.count();
      stablePasses = imageCount === previousCount ? stablePasses + 1 : 0;
      previousCount = imageCount;
      for (let index = 0; index < imageCount; index += 1) {
        await imageLocator.nth(index).evaluate(
          (image) => {
            image.scrollIntoView({ block: "center", inline: "nearest" });
          },
          undefined,
          { timeout: 2_000 },
        ).catch(() => undefined);
        await page.evaluate(() => new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }));
      }
    }
    let images: Array<{
      alt: string;
      currentSrc: string;
      loaded: boolean;
      source: string;
    }> = [];
    try {
      await expect.poll(
        async () => {
          images = await imageLocator.evaluateAll((elements) => elements.map((element) => {
            const image = element as HTMLImageElement;
            return {
              alt: image.alt,
              currentSrc: image.currentSrc,
              loaded: image.complete && image.naturalWidth > 0,
              source: image.getAttribute("src") ?? "",
            };
          }));
          return images.length > 0 && images.every((image) => (
            Boolean(image.currentSrc) && image.loaded
          ));
        },
        {
          message: `all images load on ${route}`,
          timeout: 30_000,
        },
      ).toBe(true);
    } catch (error) {
      await testInfo.attach(`failed-image-snapshot-${route.replaceAll("/", "-") || "root"}`, {
        body: JSON.stringify(images.filter((image) => !image.currentSrc || !image.loaded)),
        contentType: "application/json",
      });
      throw error;
    }
    for (const image of images) {
      expect(image.currentSrc).toBeTruthy();
      expect(image.loaded, `image failed on ${route}`).toBe(true);
      const parsed = new URL(image.currentSrc);
      expect(parsed.protocol).not.toBe("data:");
      if (parsed.protocol !== "blob:") {
        expect(allowedHosts.has(parsed.hostname), `unexpected image host ${parsed.hostname}`).toBe(true);
      }
      inventory.push({ host: parsed.hostname, path: parsed.pathname, route });
    }
  }
  await testInfo.attach("loaded-image-current-src-inventory", {
    body: JSON.stringify(inventory),
    contentType: "application/json",
  });
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
    styleAttributes: document.querySelectorAll("[style]").length,
    styleNonces: Array.from(document.querySelectorAll("style"))
      .map((element) => (element as HTMLStyleElement).nonce),
  }));
  expect(state.eventHandlers).toBe(0);
  expect(state.styleAttributes).toBe(0);
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
          && backgroundUrls.length === 1
          && backgroundUrls.every((url) => probes[url].complete && probes[url].naturalWidth > 0);
      }),
      {
        message: `card ${index + 1} images load`,
        timeout: 15_000,
      },
    ).toBe(true);
  }
}
