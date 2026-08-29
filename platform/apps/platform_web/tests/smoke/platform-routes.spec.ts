import { expect, test } from "@playwright/test";
import type { Page, Route } from "@playwright/test";
import { isNewPatch } from "../../lib/home-content-model";
import { validateLiveQaOrigin } from "../support/live-qa-origin";

async function authenticateTestUser(page: Page, extraCookies: Array<{ name: string; value: string }> = []) {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    },
    ...extraCookies.map((cookie) => ({ ...cookie, url: "http://127.0.0.1:3100" }))
  ]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => (
    Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
  ));
  expect(overflow).toBeLessThanOrEqual(2);
}

function expectOriginOnlyAuthRequest(route: Route) {
  expect(route.request().headers()["x-csrf-token"]).toBeUndefined();
}

async function trackCsrfTokenRequests(page: Page) {
  const requests: string[] = [];
  await page.route("**/api/v1/auth/csrf", async (route) => {
    requests.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({ csrf_token: `tracked.${"c".repeat(64)}` })
    });
  });
  return requests;
}

test("credential-bearing live QA accepts only the exact HTTPS production origin", () => {
  expect(validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://old-sparky.com/",
    expected: "https://old-sparky.com",
  })).toBe("https://old-sparky.com");
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://lookalike.example",
    expected: "https://old-sparky.com",
  })).toThrow(/does not match/u);
  for (const configured of [
    "http://old-sparky.com",
    "https://user:secret@old-sparky.com",
    "https://old-sparky.com/path",
    "https://old-sparky.com/?token=secret",
    "https://old-sparky.com/#fragment",
  ]) {
    expect(() => validateLiveQaOrigin({
      allowLoopback: false,
      configured,
      expected: "https://old-sparky.com",
    })).toThrow();
  }
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "http://127.0.0.1:3100",
    expected: "http://127.0.0.1:3100",
  })).toThrow(/loopback/u);
  expect(() => validateLiveQaOrigin({
    allowLoopback: false,
    configured: "https://attacker.example",
    expected: "https://attacker.example",
  })).toThrow(/old-sparky\.com/u);
  expect(() => validateLiveQaOrigin({
    allowLoopback: true,
    configured: "https://attacker.example",
    expected: "https://attacker.example",
  })).toThrow(/loopback/u);
  expect(validateLiveQaOrigin({
    allowLoopback: true,
    configured: "http://127.0.0.1:3100",
    expected: "http://127.0.0.1:3100",
  })).toBe("http://127.0.0.1:3100");
});

test("document CSP uses one fresh nonce and leaves static responses unscoped", async ({ page, request }) => {
  const first = await request.get("/");
  const second = await request.get("/");
  const firstCspHeaders = first.headersArray().filter(({ name }) => (
    name.toLowerCase() === "content-security-policy"
    || name.toLowerCase() === "content-security-policy-report-only"
  ));
  const secondCspHeaders = second.headersArray().filter(({ name }) => (
    name.toLowerCase() === "content-security-policy"
    || name.toLowerCase() === "content-security-policy-report-only"
  ));
  expect(firstCspHeaders).toHaveLength(1);
  expect(secondCspHeaders).toHaveLength(1);

  const firstPolicy = firstCspHeaders[0].value;
  const secondPolicy = secondCspHeaders[0].value;
  const firstNonce = firstPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  const secondNonce = secondPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  expect(Buffer.from(firstNonce, "base64")).toHaveLength(16);
  expect(Buffer.from(secondNonce, "base64")).toHaveLength(16);
  expect(secondNonce).not.toBe(firstNonce);
  expect(firstPolicy).toContain("script-src-attr 'none'");
  expect(firstPolicy).toContain("style-src-attr 'none'");
  expect(firstPolicy).toContain("worker-src 'self'");
  expect(firstPolicy).toContain("https://i2.ytimg.com https://i3.ytimg.com");
  expect(firstPolicy).not.toMatch(/'unsafe-|strict-dynamic|\*|\bdata:/);
  expect(first.headers()["reporting-endpoints"]).toBe(
    'csp-endpoint="/api/v1/security/csp-report"'
  );

  const attackerNonce = "attacker-csp-nonce";
  const spoofed = await request.get("/", {
    headers: {
      "Content-Security-Policy": `default-src *; script-src 'nonce-${attackerNonce}'`,
      "Content-Security-Policy-Report-Only": "default-src *",
      "x-nonce": attackerNonce,
    },
  });
  const spoofedPolicy = spoofed.headers()["content-security-policy"]
    ?? spoofed.headers()["content-security-policy-report-only"]
    ?? "";
  const proxyNonce = spoofedPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  const spoofedHtml = await spoofed.text();
  expect(Buffer.from(proxyNonce, "base64")).toHaveLength(16);
  expect(spoofedPolicy).not.toContain(attackerNonce);
  expect(spoofedPolicy).not.toContain("default-src *");
  expect(spoofedHtml).not.toContain(attackerNonce);
  expect(spoofedHtml).not.toContain("default-src *");
  expect(spoofedHtml).toContain(`nonce="${proxyNonce}"`);

  const querySpoofed = await request.get("/?_rsc=attacker");
  expect(
    querySpoofed.headers()["content-security-policy"]
      ?? querySpoofed.headers()["content-security-policy-report-only"]
  ).toBeTruthy();

  const navigation = await page.goto("/");
  expect(navigation).not.toBeNull();
  const navigationHeaders = navigation!.headers();
  const navigationPolicy = navigationHeaders["content-security-policy"]
    ?? navigationHeaders["content-security-policy-report-only"]
    ?? "";
  const navigationNonce = navigationPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  const nonceState = await page.evaluate((nonce) => ({
    inlineScriptsWithoutNonce: Array.from(document.querySelectorAll<HTMLScriptElement>("script:not([src])"))
      .filter((node) => node.nonce !== nonce).length,
    inlineStylesWithoutNonce: Array.from(document.querySelectorAll<HTMLStyleElement>("style"))
      .filter((node) => node.nonce !== nonce).length,
    styleAttributes: document.querySelectorAll("[style]").length,
  }), navigationNonce);
  expect(nonceState).toEqual({
    inlineScriptsWithoutNonce: 0,
    inlineStylesWithoutNonce: 0,
    styleAttributes: 0,
  });

  const bracketRscHeaders: Array<Promise<Record<string, string>>> = [];
  page.on("response", (response) => {
    const responseUrl = new URL(response.url());
    if (
      responseUrl.pathname.endsWith("/tournaments/night-veil-open-5/bracket")
      && response.request().headers().rsc === "1"
    ) {
      bracketRscHeaders.push(response.allHeaders());
    }
  });
  const detailNavigation = await page.goto("/tournaments/night-veil-open-5");
  expect(detailNavigation).not.toBeNull();
  const detailHeaders = detailNavigation!.headers();
  const detailPolicy = detailHeaders["content-security-policy"]
    ?? detailHeaders["content-security-policy-report-only"]
    ?? "";
  const documentNonce = detailPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  await page.locator(".bracket-open-link").click();
  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5\/bracket$/);
  await expect.poll(() => bracketRscHeaders.length).toBeGreaterThan(0);
  for (const responseHeaders of await Promise.all(bracketRscHeaders)) {
    expect(responseHeaders["content-security-policy"]).toBeUndefined();
    expect(responseHeaders["content-security-policy-report-only"]).toBeUndefined();
    expect(responseHeaders["reporting-endpoints"]).toBeUndefined();
  }
  const bracketStyle = page.locator(".bracket-wrap > style");
  await expect(bracketStyle).toHaveCount(1);
  expect(await bracketStyle.evaluate((node) => (node as HTMLStyleElement).nonce)).toBe(documentNonce);
  await page.getByLabel("Приблизить").click();
  expect(await bracketStyle.evaluate((node) => (node as HTMLStyleElement).nonce)).toBe(documentNonce);
  const softNavigationStyleAttributes = await page.locator("[style]").evaluateAll((nodes) => (
    nodes.map((node) => ({
      className: node.getAttribute("class"),
      style: node.getAttribute("style"),
      tagName: node.tagName,
    }))
  ));
  expect(softNavigationStyleAttributes).toEqual([]);
  const announcerState = await page.evaluate(() => {
    const host = document.querySelector("next-route-announcer");
    const announcer = host?.shadowRoot?.querySelector("#__next-route-announcer__");
    const stylesheet = host?.shadowRoot?.querySelector("style");
    return {
      announcerStyle: announcer?.getAttribute("style") ?? null,
      hostStyle: host?.getAttribute("style") ?? null,
      nonce: (stylesheet as HTMLStyleElement | null)?.nonce ?? null,
      role: announcer?.getAttribute("role") ?? null,
    };
  });
  expect(announcerState).toEqual({
    announcerStyle: null,
    hostStyle: null,
    nonce: documentNonce,
    role: "alert",
  });

  const notFound = await request.get("/csp-nonce-not-found");
  expect(notFound.status()).toBe(404);
  const notFoundPolicy = notFound.headers()["content-security-policy"]
    ?? notFound.headers()["content-security-policy-report-only"]
    ?? "";
  const notFoundNonce = notFoundPolicy.match(/'nonce-([A-Za-z0-9+/]+={0,2})'/)?.[1] ?? "";
  const notFoundHtml = await notFound.text();
  expect(Buffer.from(notFoundNonce, "base64")).toHaveLength(16);
  expect(notFoundHtml).not.toMatch(/<[^>]+\sstyle\s*=/iu);
  for (const tag of notFoundHtml.match(/<(?:script|style)\b[^>]*>/giu) ?? []) {
    expect(tag).toContain(`nonce="${notFoundNonce}"`);
  }
  const notFoundNavigation = await page.goto("/csp-nonce-not-found");
  expect(notFoundNavigation?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "404", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "На главную" })).toBeVisible();
  await expect(page.locator("[style]")).toHaveCount(0);

  const staticAsset = await request.get("/assets/main_logo/old-sparky-arena-logo-v3.webp");
  expect(staticAsset.headers()["content-security-policy"]).toBeUndefined();
  expect(staticAsset.headers()["content-security-policy-report-only"]).toBeUndefined();
  expect(staticAsset.headers()["reporting-endpoints"]).toBeUndefined();

  const appleIcon = await request.get("/apple-icon.png");
  expect(appleIcon.status()).toBe(200);
  expect(appleIcon.headers()["content-security-policy"]).toBeUndefined();
  expect(appleIcon.headers()["content-security-policy-report-only"]).toBeUndefined();
  expect(appleIcon.headers()["reporting-endpoints"]).toBeUndefined();
});

async function expectSharedBracketTeamTemplate(page: Page) {
  const match = page.getByTestId("bracket-match").first();
  await expect(match.locator(".team-slot-content")).toHaveCount(2);
  const geometry = await match.evaluate((element) => {
    const frame = element.querySelector<HTMLElement>(".match-frame")!;
    const meta = element.querySelector<HTMLElement>(".match-meta")!;
    const rows = Array.from(element.querySelectorAll<HTMLElement>(".team-slot-content"));
    const firstScore = rows[0].querySelector<HTMLElement>(".score")!;
    return {
      frameRight: frame.getBoundingClientRect().right,
      metaDividerColor: getComputedStyle(meta).borderBottomColor,
      rowDividerColor: getComputedStyle(rows[1], "::before").backgroundColor,
      rowHeights: rows.map((row) => Number.parseFloat(getComputedStyle(row).height)),
      cells: rows.map((row) => {
        const rowBox = row.getBoundingClientRect();
        const seed = row.querySelector<HTMLElement>(".seed")!;
        const seedValue = row.querySelector<HTMLElement>(".seed-value")!;
        const name = row.querySelector<HTMLElement>(".team-name")!;
        const nameCopy = row.querySelector<HTMLElement>(".team-name-copy")!;
        const label = row.querySelector<HTMLElement>(".team-name-label")!;
        const strength = row.querySelector<HTMLElement>(".team-name small");
        const score = row.querySelector<HTMLElement>(".score")!;
        const scoreValue = row.querySelector<HTMLElement>(".score-value")!;
        const seedValueBox = seedValue.getBoundingClientRect();
        const nameCopyBox = nameCopy.getBoundingClientRect();
        const labelBox = label.getBoundingClientRect();
        const strengthBox = strength?.getBoundingClientRect() ?? null;
        const scoreBox = score.getBoundingClientRect();
        const scoreValueBox = scoreValue.getBoundingClientRect();
        const labelStyle = getComputedStyle(label);
        return {
          seedCenterDelta: Math.abs((seedValueBox.top + seedValueBox.height / 2) - (rowBox.top + rowBox.height / 2)),
          seedJustify: getComputedStyle(seed).justifyContent,
          nameCenterDelta: Math.abs((nameCopyBox.top + nameCopyBox.height / 2) - (rowBox.top + rowBox.height / 2)),
          nameJustify: getComputedStyle(name).justifyContent,
          labelFontSize: Number.parseFloat(labelStyle.fontSize),
          labelLineHeight: Number.parseFloat(labelStyle.lineHeight),
          strengthBottomDelta: strengthBox ? Math.abs(strengthBox.bottom - labelBox.bottom) : 0,
          scoreCenterXDelta: Math.abs((scoreValueBox.left + scoreValueBox.width / 2) - (scoreBox.left + scoreBox.width / 2)),
          scoreCenterYDelta: Math.abs((scoreValueBox.top + scoreValueBox.height / 2) - (rowBox.top + rowBox.height / 2)),
          scoreJustify: getComputedStyle(score).justifyContent,
        };
      }),
      scoreBorderColor: getComputedStyle(firstScore).borderLeftColor,
      scoreBorderWidth: getComputedStyle(firstScore).borderLeftWidth,
      scoreRightInset: frame.getBoundingClientRect().right - firstScore.getBoundingClientRect().right,
    };
  });

  expect(geometry.rowHeights).toEqual([40, 40]);
  expect(geometry.cells.every((cells) => cells.seedCenterDelta <= 0.5)).toBe(true);
  expect(geometry.cells.every((cells) => cells.seedJustify === "flex-start")).toBe(true);
  expect(geometry.cells.every((cells) => cells.nameCenterDelta <= 0.5)).toBe(true);
  expect(geometry.cells.every((cells) => cells.nameJustify === "flex-start")).toBe(true);
  expect(geometry.cells.every((cells) => cells.labelLineHeight >= cells.labelFontSize * 1.2)).toBe(true);
  expect(geometry.cells.every((cells) => cells.strengthBottomDelta <= 0.5)).toBe(true);
  expect(geometry.cells.every((cells) => cells.scoreCenterXDelta <= 0.1)).toBe(true);
  expect(geometry.cells.every((cells) => cells.scoreCenterYDelta <= 0.5)).toBe(true);
  expect(geometry.cells.every((cells) => cells.scoreJustify === "center")).toBe(true);
  expect(geometry.scoreBorderWidth).toBe("1px");
  expect(geometry.scoreBorderColor).toBe(geometry.metaDividerColor);
  expect(geometry.rowDividerColor).toBe(geometry.metaDividerColor);
  expect(geometry.scoreRightInset).toBeLessThanOrEqual(1.1);
}

async function waitForAllTournamentCardAssets(page: Page) {
  const cards = page.getByTestId("tournament-card");
  const count = await cards.count();
  for (let index = 0; index < count; index += 1) {
    const card = cards.nth(index);
    await card.scrollIntoViewIfNeeded();
    await expect.poll(async () => card.evaluate(async (cardElement) => {
      const images = Array.from(cardElement.querySelectorAll<HTMLImageElement>("img"));
      await Promise.all(images.map(async (image) => {
        if (!image.complete) {
          await new Promise<void>((resolve) => {
            image.addEventListener("load", () => resolve(), { once: true });
            image.addEventListener("error", () => resolve(), { once: true });
          });
        }
        await image.decode().catch(() => undefined);
      }));

      const bannerImage = cardElement.querySelector<HTMLImageElement>(".card-banner-media");
      return Boolean(bannerImage)
        && images.length > 0
        && images.every((image) => image.complete && image.naturalWidth > 0);
    })).toBe(true);
  }
}

const routes = [
  { path: "/", name: "home", title: "Главная | Old Sparky Arena", visibleText: "Последние патчи Deadlock" },
  { path: "/patches/1836506165584438", name: "patch detail", title: "Патч Deadlock | Old Sparky Arena", visibleText: "Общие изменения" },
  { path: "/info", name: "information", title: "Инфо | Old Sparky Arena", visibleText: "Как пользоваться" },
  { path: "/privacy", name: "privacy", title: "Политика конфиденциальности | Old Sparky Arena", visibleText: "Какие данные мы получаем" },
  { path: "/terms", name: "terms", title: "Условия использования | Old Sparky Arena", visibleText: "Назначение платформы" },
  { path: "/tournaments", name: "tournaments", title: "Турниры | Old Sparky Arena", visibleText: "Night Veil Open #5" },
  { path: "/tournaments/new", name: "create tournament", title: "Создать турнир | Old Sparky Arena", visibleText: "Создать турнир" },
  { path: "/tournaments/night-veil-open-5", name: "tournament detail", title: "Турнир | Old Sparky Arena", visibleText: "Описание турнира" },
  { path: "/tournaments/night-veil-open-5/bracket", name: "bracket", title: "Сетка турнира | Old Sparky Arena", visibleText: "Night Veil Open #5" },
  { path: "/auth/login", name: "login", title: "Вход | Old Sparky Arena", visibleText: "Вход" },
  { path: "/auth/register", name: "register", title: "Создать аккаунт | Old Sparky Arena", visibleText: "Создать аккаунт" },
  { path: "/profile/me", name: "my profile", title: "Мой профиль | Old Sparky Arena", visibleText: "Профиль игрока" },
  { path: "/profile/lisalexy", name: "public profile", title: "Профиль игрока | Old Sparky Arena", visibleText: "lisalexy" }
] as const;

for (const route of routes) {
  test(`${route.name} route renders key content`, async ({ page }) => {
    await page.goto(route.path);

    await expect(page.getByText(route.visibleText).filter({ visible: true }).first()).toBeVisible();
    await expect(page).toHaveTitle(route.title);
    await expect(page.getByRole("banner")).toBeVisible();
    await expect(page.getByRole("contentinfo")).toBeVisible();
  });
}

test("public navigation does not prefetch auth documents or Turnstile", async ({ page }) => {
  const forbiddenRequests: string[] = [];
  page.on("request", (request) => {
    const parsed = new URL(request.url());
    if (
      parsed.hostname === "challenges.cloudflare.com"
      || ["/auth/login", "/auth/register"].includes(parsed.pathname)
    ) {
      forbiddenRequests.push(`${request.method()} ${parsed.origin}${parsed.pathname}`);
    }
  });

  await page.goto("/");
  await expect(page.getByRole("banner").getByRole("link", { name: "Войти", exact: true })).toBeVisible();
  await page.waitForTimeout(1_000);

  expect(forbiddenRequests).toEqual([]);
});

test("site footer exposes valid navigation and project attribution", async ({ page }) => {
  await page.goto("/auth/login");

  const header = page.getByRole("banner");
  await expect(header).toHaveCSS("border-bottom-width", "1px");
  await expect(page.locator(".hero-wrap")).toHaveCSS("border-bottom-width", "0px");
  await expect(header.locator(".brand-title")).toHaveText("OLD SPARKY");
  await expect(header.locator(".brand-title")).toHaveCSS("white-space", "nowrap");
  await expect(header.locator(".brand-sub")).toHaveText("ARENA");
  await expect(header.locator(".brand-mark")).toHaveAttribute("src", "/assets/main_logo/old-sparky-arena-logo-v3.webp");
  await expect(header.locator(".brand-mark")).toHaveAttribute("srcset", /old-sparky-arena-logo-v3-64\.webp 64w/u);
  const footer = page.getByRole("contentinfo");
  await expect(footer).toHaveCount(1);
  await expect(footer.getByRole("navigation", { name: "Платформа" })).toBeVisible();
  await expect(footer.getByRole("navigation", { name: "Аккаунт" })).toBeVisible();
  await expect(footer.getByRole("navigation", { name: "Deadlock" })).toBeVisible();
  await expect(footer.getByRole("link", { name: "Турниры" })).toHaveAttribute("href", "/tournaments");
  const platformFooterNav = footer.getByRole("navigation", { name: "Платформа" });
  await expect(platformFooterNav.getByRole("link", { name: "Главная" })).toHaveAttribute("href", "/");
  await expect(platformFooterNav.getByRole("link", { name: "Инфо" })).toHaveAttribute("href", "/info");
  await expect(platformFooterNav.getByRole("link", { name: "Конфиденциальность" })).toHaveAttribute("href", "/privacy");
  await expect(platformFooterNav.getByRole("link", { name: "Условия использования" })).toHaveAttribute("href", "/terms");
  await expect(footer.getByRole("link", { name: "Мой профиль" })).toHaveAttribute("href", "/profile/me");
  await expect(footer.getByRole("link", { name: "Официальный сайт игры" })).toHaveAttribute("target", "_blank");
  await expect(footer).toContainText("Проект не связан с Valve и не одобрен ею.");

  await page.setViewportSize({ width: 390, height: 844 });
  await footer.scrollIntoViewIfNeeded();
  const footerBox = await footer.boundingBox();
  expect(footerBox?.y).toBeLessThan(844);
  expect(await page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
  ))).toBe(true);
});

test("public discovery documents expose the canonical origin without publishing support email", async ({ request }) => {
  const [robots, sitemap, manifest, security, privacy, terms] = await Promise.all([
    request.get("/robots.txt"),
    request.get("/sitemap.xml"),
    request.get("/manifest.webmanifest"),
    request.get("/.well-known/security.txt"),
    request.get("/privacy"),
    request.get("/terms")
  ]);

  expect(robots.ok()).toBe(true);
  expect(await robots.text()).toContain("Sitemap: https://old-sparky.com/sitemap.xml");
  expect(sitemap.ok()).toBe(true);
  const sitemapBody = await sitemap.text();
  expect(sitemapBody).toContain("https://old-sparky.com/privacy");
  expect(sitemapBody).toContain("https://old-sparky.com/terms");
  expect(manifest.ok()).toBe(true);
  expect((await manifest.json()).name).toBe("Old Sparky Arena");
  expect(security.ok()).toBe(true);
  const publicContactDocuments = [await security.text(), await privacy.text(), await terms.text()];
  expect(publicContactDocuments[0]).toContain("Contact: https://old-sparky.com/info#support");
  expect(publicContactDocuments.every((document) => !document.includes("support@old-sparky.com"))).toBe(true);
  expect(publicContactDocuments.every((document) => !document.includes("mailto:"))).toBe(true);
});

test("header marks only the selected tournament navigation item", async ({ page }) => {
  await page.goto("/tournaments");

  const tournamentLink = page.getByRole("banner").getByRole("link", { name: "Турниры", exact: true });
  const createLink = page.getByRole("banner").getByRole("link", { name: "Создать турнир", exact: true });
  await expect(page.getByLabel("Главная навигация").getByRole("link", { name: "Создать турнир" })).toHaveCount(0);
  await expect(createLink).toHaveClass(/header-create-button/);
  await expect(tournamentLink).toHaveClass(/nav-link-active/);
  await expect(createLink).not.toHaveClass(/active/);
  await expect(tournamentLink).toHaveCSS("box-shadow", "none");
  await expect.poll(() => tournamentLink.evaluate((element) => getComputedStyle(element, "::after").height).catch(() => "")).toBe("2px");

  await page.goto("/tournaments/new");
  await expect(tournamentLink).not.toHaveClass(/nav-link-active/);
  await expect(createLink).toHaveClass(/active/);

  await page.goto("/");
  await expect(page.getByRole("banner").getByRole("link", { name: "Главная", exact: true })).toHaveClass(/nav-link-active/);

  await page.goto("/info");
  await expect(page.getByRole("banner").getByRole("link", { name: "Инфо", exact: true })).toHaveClass(/nav-link-active/);
});

test("mobile authenticated header keeps compact icon actions beside the brand", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Compact authenticated header is owned by the phone viewport.");
  await page.setViewportSize({ width: 320, height: 900 });
  await authenticateTestUser(page);
  await page.goto("/tournaments");

  const header = page.getByRole("banner");
  const brand = header.locator(".brand");
  const actions = header.locator(".header-actions");
  const operations = header.getByRole("link", { name: "Operations" });
  const profileLink = header.getByRole("link", { name: "Профиль: lisalexy" });
  const createLink = header.getByRole("link", { name: "Создать турнир" });

  expect(await header.locator(".header-operations-label, .header-profile-label, .header-create-label").evaluateAll((labels) => (
    labels.length === 3 && labels.every((label) => getComputedStyle(label).display === "none")
  ))).toBe(true);
  for (const action of [operations, profileLink, createLink]) {
    await expect(action).toBeVisible();
    const box = await action.boundingBox();
    expect(box).not.toBeNull();
    expect(Math.abs(box!.width - box!.height)).toBeLessThanOrEqual(1);
  }
  const brandBox = await brand.boundingBox();
  const actionsBox = await actions.boundingBox();
  expect(brandBox).not.toBeNull();
  expect(actionsBox).not.toBeNull();
  expect(Math.abs((brandBox!.y + brandBox!.height / 2) - (actionsBox!.y + actionsBox!.height / 2))).toBeLessThanOrEqual(2);
  await expectNoHorizontalOverflow(page);
});

test("information page uses the concise rules title", async ({ page }) => {
  await page.goto("/info");
  await expect(page.getByRole("heading", { name: "Правила", exact: true })).toBeVisible();
  await expect(page.getByText("Правила платформы", { exact: true })).toHaveCount(0);
});

test("legal documents cover Steam identity and optional email", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Legal auth copy is viewport-independent.");
  await page.goto("/privacy");
  await expect(page.getByText(/подтверждённый SteamID64 и факт привязки Steam/u)).toBeVisible();
  await expect(page.getByText("Последнее обновление: 13 августа 2026 года")).toBeVisible();

  await page.goto("/terms");
  await expect(page.getByText(/Для аккаунта, созданного через Steam, email не обязателен/u)).toBeVisible();
});

test("site pages keep the same space between hero and working area", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Shared desktop geometry is covered once.");
  await page.setViewportSize({ width: 1920, height: 1080 });

  for (const path of ["/tournaments", "/info", "/profile/me", "/tournaments/night-veil-open-5"]) {
    await page.goto(path);
    const hero = page.locator(".hero-wrap:visible").last();
    const main = page.locator("main.main:visible").last();
    await expect(hero).toBeVisible();
    await expect(main).toBeVisible();
    await expect(main).toHaveCSS("padding-top", "38px");
    await expect.poll(async () => {
      const heroBox = await hero.boundingBox();
      const mainBox = await main.boundingBox();
      if (!heroBox || !mainBox) {
        return Number.POSITIVE_INFINITY;
      }
      return Math.abs(mainBox.y - (heroBox.y + heroBox.height));
    }, { message: `${path} shared hero rhythm` }).toBeLessThanOrEqual(1);
  }
});

test("info explains public and monthly private tournament creation", async ({ page }) => {
  await page.goto("/info");

  const publicQuestion = page.getByText("Как создать публичный турнир?", { exact: true });
  const privateQuestion = page.getByText("Сколько приватных турниров можно создавать?", { exact: true });
  await publicQuestion.click();
  await privateQuestion.click();
  await expect(page.getByText("Обратитесь к администрации, чтобы получить разрешение.", { exact: true })).toBeVisible();
  await expect(page.getByText(/Лимит сбрасывается первого числа каждого месяца/)).toBeVisible();
});

test("authenticated player can open a private tournament with an invite code", async ({ page }) => {
  await authenticateTestUser(page);
  let claimBody: Record<string, unknown> | null = null;
  await page.route("**/api/v1/tournaments/invites/claim", async (route) => {
    claimBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        tournament: { slug: "night-veil-open-5", name: "Night Veil Open #5" },
        participant: null,
        invite: { code: "NIGHTVEIL" }
      })
    });
  });

  await page.goto("/tournaments");
  await page.getByLabel("Код приглашения").fill("nightveil");

  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5$/);
  expect(claimBody).toMatchObject({ code: "NIGHTVEIL", entry_type: "solo", team_name: null });
});

test("tournament list renders public tournaments and hides invite-only tournaments without code", async ({ page }) => {
  await page.goto("/tournaments");

  await expect(page.getByRole("heading", { name: "Night Veil Open #5" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Citadel Clash #3" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Geist Circuit Invitational" })).toHaveCount(0);
});

test("site header ignores obsolete client auth snapshots", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "platform:last-known-user:v1",
      JSON.stringify({ id: "u_saved", display_name: "Saved Player" })
    );
  });
  await page.goto("/tournaments");

  const banner = page.getByRole("banner");
  await expect(banner.getByRole("link", { name: /Saved Player/ })).toHaveCount(0);
  await expect(banner.getByText("Проверяем...")).toHaveCount(0);
  await expect(banner.getByLabel("Проверяем сессию")).toHaveCount(0);
  await expect(banner.getByRole("link", { name: "Войти" })).toBeVisible();
  expect(await page.evaluate(() => window.localStorage.getItem("platform:last-known-user:v1"))).not.toBeNull();
});

test("server-rendered header resolves auth states before client JavaScript runs", async ({ browser }) => {
  async function openWithoutJavaScript(cookies: Array<{ name: string; value: string }> = []) {
    const context = await browser.newContext({ javaScriptEnabled: false });
    if (cookies.length) {
      await context.addCookies(cookies.map((cookie) => ({
        ...cookie,
        url: "http://127.0.0.1:3100"
      })));
    }
    const renderedPage = await context.newPage();
    await renderedPage.goto("/tournaments");
    return { context, renderedPage };
  }

  const anonymous = await openWithoutJavaScript();
  await expect(anonymous.renderedPage.getByRole("banner").getByRole("link", { name: "Войти" })).toBeVisible();
  await expect(anonymous.renderedPage.getByRole("banner").getByRole("link", { name: /Профиль:/u })).toHaveCount(0);
  await anonymous.context.close();

  const authenticated = await openWithoutJavaScript([{ name: "deadlock_platform_session", value: "smoke-session" }]);
  await expect(authenticated.renderedPage.getByRole("banner").getByRole("link", { name: "Профиль: lisalexy" })).toBeVisible();
  await expect(authenticated.renderedPage.getByRole("banner").getByRole("link", { name: "Войти" })).toHaveCount(0);
  await authenticated.context.close();

  const unavailable = await openWithoutJavaScript([
    { name: "deadlock_platform_session", value: "smoke-session" },
    { name: "users-me-unavailable-smoke", value: "1" }
  ]);
  await expect(unavailable.renderedPage.getByRole("banner").getByRole("button", { name: "Повторить проверку сессии" })).toBeVisible();
  await expect(unavailable.renderedPage.getByRole("banner").getByRole("link", { name: "Войти" })).toHaveCount(0);
  await unavailable.context.close();

  const malformed = await openWithoutJavaScript([
    { name: "deadlock_platform_session", value: "smoke-session" },
    { name: "users-me-malformed-smoke", value: "1" }
  ]);
  await expect(malformed.renderedPage.getByRole("banner").getByRole("button", { name: "Повторить проверку сессии" })).toBeVisible();
  await expect(malformed.renderedPage.getByRole("banner").getByRole("link", { name: "Профиль: Malformed User" })).toHaveCount(0);
  await expect(malformed.renderedPage.getByRole("banner").getByRole("link", { name: "Войти" })).toHaveCount(0);
  await malformed.context.close();
});

test("authenticated header stays resolved across client navigation without a browser users-me request", async ({ page }) => {
  await authenticateTestUser(page);
  let browserMeRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/users/me") {
      browserMeRequests += 1;
    }
  });
  await page.goto("/tournaments");
  const banner = page.getByRole("banner");
  await expect(banner.getByRole("link", { name: "Профиль: lisalexy" })).toBeVisible();
  await banner.getByRole("link", { name: "Инфо" }).click();
  await expect(page).toHaveURL(/\/info$/u);
  await expect(banner.getByRole("link", { name: "Профиль: lisalexy" })).toBeVisible();
  await expect(banner.getByText("Проверяем...")).toHaveCount(0);
  expect(browserMeRequests).toBe(0);
});

test("status and rank filters operate on typed tournament data", async ({ page }) => {
  await page.goto("/tournaments");

  await page.getByRole("main").getByTestId("status-filter").selectOption("registration_closed");
  await expect(page.getByRole("main").getByTestId("status-filter")).not.toBeFocused();
  await expect(page.getByRole("heading", { name: "Citadel Clash #3" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Night Veil Open #5" })).toHaveCount(0);

  await page.getByRole("button", { name: "Сбросить" }).click();
  await page.getByTestId("rank-filter").selectOption("r9");
  await expect(page.getByTestId("rank-filter")).not.toBeFocused();
  await expect(page.getByRole("heading", { name: "Night Veil Open #5" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Citadel Clash #3" })).toHaveCount(0);
});

test("tournament filters keep invite and dropdown controls equal while search stays wider", async ({ page }) => {
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.goto("/tournaments");
  await expect(page.getByRole("main").getByTestId("status-filter")).toBeVisible();

  const panelBox = await page.getByRole("main").locator(".filters-panel").boundingBox();
  const searchBox = await page.getByTestId("tournament-search-filter").locator("..").locator("..").boundingBox();
  const inviteBox = await page.getByTestId("tournament-invite-code").locator("..").locator("..").boundingBox();
  const statusBox = await page.getByRole("main").getByTestId("status-filter").locator("..").locator("..").boundingBox();
  const rankBox = await page.getByRole("main").getByTestId("rank-filter").locator("..").locator("..").boundingBox();
  const scopeBox = await page.getByRole("main").getByTestId("tournament-scope-filter").locator("..").locator("..").boundingBox();
  const dateBox = await page.getByRole("main").getByTestId("date-sort-filter").boundingBox();

  expect(searchBox?.width).toBeGreaterThan(statusBox?.width ?? 0);
  for (const box of [inviteBox, rankBox, scopeBox, dateBox]) {
    expect(Math.abs((box?.width ?? 0) - (statusBox?.width ?? 0))).toBeLessThanOrEqual(1);
  }

  const dateTopGap = (dateBox?.y ?? 0) - (panelBox?.y ?? 0);
  const dateBottomGap = ((panelBox?.y ?? 0) + (panelBox?.height ?? 0))
    - ((dateBox?.y ?? 0) + (dateBox?.height ?? 0));
  expect(Math.abs(dateTopGap - dateBottomGap)).toBeLessThanOrEqual(1);
});

test("tournament filters and profile actions do not overflow a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto("/tournaments");
  await expect(page.getByRole("main").locator(".filters-panel")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/profile/me");
  await expect(page.getByTestId("profile-save-settings-button")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
});

test("mobile empty tournament result is centered vertically", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile empty-state geometry is covered once.");
  await page.goto("/tournaments");
  await page.getByTestId("tournament-search-filter").fill("no-such-tournament-2026");

  const grid = page.getByTestId("tournaments-grid");
  const emptyState = grid.locator(".tournament-list-state");
  await expect(emptyState).toHaveText("Турниров пока нет.");
  await expect(grid).toHaveClass(/tournaments-grid-empty/);
  const centers = await grid.evaluate((element) => {
    const state = element.querySelector(".tournament-list-state")!;
    const gridBox = element.getBoundingClientRect();
    const stateBox = state.getBoundingClientRect();
    return {
      delta: Math.abs((gridBox.top + gridBox.height / 2) - (stateBox.top + stateBox.height / 2)),
      minHeight: Number.parseFloat(getComputedStyle(element).minHeight)
    };
  });
  expect(centers.minHeight).toBeGreaterThanOrEqual(220);
  expect(centers.delta).toBeLessThanOrEqual(1);
});

test("mobile profile banners show their complete descriptions", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile profile banner clipping is covered once.");
  await authenticateTestUser(page);
  await page.goto("/profile/me");

  for (const tab of ["Турнирный профиль", "Профиль капитана", "Аккаунт"]) {
    await page.getByRole("button", { name: tab, exact: true }).click();
    const description = page.locator(".captain-banner:visible .description-text");
    await expect(description).toBeVisible();
    await expect(description).toHaveCSS("white-space", "normal");
    await expect(description).toHaveCSS("overflow", "visible");
    expect(await description.evaluate((element) => element.scrollHeight <= element.clientHeight + 1)).toBe(true);
  }
});

test("admin console loads operational data and keeps audited actions explicit", async ({ page }) => {
  let overrideBody: Record<string, unknown> | null = null;
  let deleteBody: Record<string, unknown> | null = null;
  let deleteUserBody: Record<string, unknown> | null = null;
  let creditsBody: Record<string, unknown> | null = null;
  let roleBody: Record<string, unknown> | null = null;
  let tournament = {
    id: "t_admin_cup",
    slug: "admin-cup",
    name: "Admin Cup",
    description: "Admin workflow smoke",
    visibility: "invite_only",
    status: "in_progress",
    format_slug: "solo",
    organizer_user_id: "u_organizer",
    organizer_display_name: "Tournament Owner",
    participant_count: 18,
    max_participants: 32,
    allowed_ranks: ["r1", "r2"],
    has_locked_deadlock_roster: true,
    created_at: "2026-06-08T12:00:00Z",
    available_next_statuses: ["completed", "cancelled"],
    match_count: 4,
    latest_round_number: 2,
    unfinished_match_count: 1,
    completed_match_count: 3,
    cancelled_match_count: 0,
    admin_override_warning: "One match still needs review." as string | null,
    admin_recovery_hint: "Open tournament detail to repair the latest round."
  };
  let managedUser = {
    id: "u_managed",
    email: "managed@example.com",
    display_name: "Managed Player",
    status: "active",
    created_at: "2026-06-01T12:00:00Z",
    roles: [] as string[],
    can_create_public_tournaments: false,
    public_tournament_credits: 0,
    private_tournament_credits: 0
  };

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.route("**/api/v1/admin/overview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        users_total: 42,
        tournaments_total: 12,
        audit_events_total: 87
      })
    });
  });

  await page.route("**/api/v1/admin/tournaments**", async (route) => {
    if (route.request().method() === "DELETE") {
      deleteBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (route.request().method() === "PATCH") {
      overrideBody = route.request().postDataJSON() as Record<string, unknown>;
      tournament = {
        ...tournament,
        status: String(overrideBody.status ?? tournament.status),
        visibility: String(overrideBody.visibility ?? tournament.visibility),
        admin_override_warning: null
      };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(tournament) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([tournament]) });
  });

  await page.route("**/api/v1/admin/audit-logs**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        id: 87,
        action: "admin.tournament.override",
        subject_type: "tournament",
        subject_id: "t_admin_cup",
        payload: {
          tournament_slug: "admin-cup",
          note: "Resolve disputed bracket state."
        },
        actor_display_name: "Platform Admin",
        actor_email: "admin@example.com",
        created_at: "2026-06-08T13:00:00Z"
      }])
    });
  });

  await page.route("**/api/v1/admin/preprod-test-runs**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{
        marker: "preprod260620120000abcd",
        status: "passed",
        origin: "http://127.0.0.1",
        requested_users: 10000,
        created_users: 10000,
        tournaments_created: 1,
        active_participants: 10000,
        teams_count: 128,
        matches_count: 127,
        report_path: "/tmp/preprod-report.json",
        report: {
          preference_metrics: {
            starter_preference_slots_fully_honored_rate_percent: 71.25
          },
          optimization_summary: {
            spread_percent: 2.4
          }
        },
        cleanup_state: {},
        started_at: "2026-06-20T12:00:00Z",
        finished_at: "2026-06-20T12:20:00Z",
        created_at: "2026-06-20T12:00:00Z",
        updated_at: "2026-06-20T12:20:00Z"
      }])
    });
  });

  await page.route("**/api/v1/admin/users**", async (route) => {
    if (route.request().method() === "DELETE") {
      deleteUserBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (route.request().method() === "PATCH") {
      if (route.request().url().endsWith("/admin-role")) {
        roleBody = route.request().postDataJSON() as Record<string, unknown>;
        managedUser = {
          ...managedUser,
          roles: (roleBody as Record<string, unknown>).is_admin ? ["admin"] : []
        };
      } else {
        creditsBody = route.request().postDataJSON() as Record<string, unknown>;
        managedUser = {
          ...managedUser,
          can_create_public_tournaments: Number((creditsBody as Record<string, unknown>).public_tournament_credits) > 0,
          public_tournament_credits: Number((creditsBody as Record<string, unknown>).public_tournament_credits),
          private_tournament_credits: Number((creditsBody as Record<string, unknown>).private_tournament_credits)
        };
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(managedUser) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([managedUser]) });
  });

  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Операционный центр платформы" })).toBeVisible();
  await expect(page.getByText("42", { exact: true })).toBeVisible();
  await expect(page.getByText("87", { exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-tournament-admin-cup")).toBeVisible();
  await page.getByTestId("admin-tournament-admin-cup").click();

  const applyOverride = page.getByTestId("admin-apply-override");
  await expect(applyOverride).toBeDisabled();
  await page.getByTestId("admin-status-override").selectOption("completed");
  await expect(applyOverride).toBeDisabled();
  await page.getByTestId("admin-override-note").fill("Resolve disputed bracket state.");
  await expect(applyOverride).toBeEnabled();
  await applyOverride.click();
  await expect(page.getByText("Override сохранен для Admin Cup.")).toBeVisible();
  expect(overrideBody).toMatchObject({
    status: "completed",
    visibility: null,
    note: "Resolve disputed bracket state."
  });

  await page.getByTestId("admin-status-override").selectOption("registration_open");
  await expect(page.getByTestId("admin-schedule-editor")).toBeVisible();
  await expect(page.getByTestId("admin-registration-closes-at")).not.toHaveValue("");
  await page.getByTestId("admin-override-note").fill("Reopen with a replacement workflow schedule.");
  overrideBody = null;
  await page.getByTestId("admin-apply-override").click();
  await expect.poll(() => overrideBody && String(overrideBody.status)).toBe("registration_open");
  expect(overrideBody).toMatchObject({
    status: "registration_open",
    visibility: null,
    note: "Reopen with a replacement workflow schedule."
  });
  const reopenPayload = overrideBody as Record<string, unknown> | null;
  expect(typeof reopenPayload?.registration_closes_at).toBe("string");
  expect(typeof reopenPayload?.ready_check_starts_at).toBe("string");
  expect(typeof reopenPayload?.starts_at).toBe("string");

  const deleteTournament = page.getByTestId("admin-delete-tournament");
  await expect(deleteTournament).toBeDisabled();
  await page.getByTestId("admin-delete-tournament-confirmation").fill("Admin Cup");
  await page.getByTestId("admin-delete-tournament-note").fill("Remove obsolete smoke tournament.");
  await expect(deleteTournament).toBeEnabled();
  await deleteTournament.click();
  await expect(page.getByTestId("admin-tournament-admin-cup")).toHaveCount(0);
  expect(deleteBody).toMatchObject({
    confirmation_name: "Admin Cup",
    note: "Remove obsolete smoke tournament."
  });

  await page.getByRole("tab", { name: /Права пользователей/ }).click();
  await expect(page.getByTestId("admin-user-u_managed")).toBeVisible();
  await page.getByTestId("admin-user-u_managed").click();
  await page.getByTestId("admin-public-credits").fill("2");
  await page.getByTestId("admin-private-credits").fill("4");
  await page.getByTestId("admin-override-note").fill("Approved expanded tournament capacity.");
  await page.getByTestId("admin-save-credits").click();
  await expect(page.getByText("Доступные турниры сохранены для Managed Player.")).toBeVisible();
  expect(creditsBody).toMatchObject({
    public_tournament_credits: 2,
    private_tournament_credits: 4,
    note: "Approved expanded tournament capacity."
  });

  await page.getByTestId("admin-override-note").fill("Add another platform administrator.");
  await page.getByTestId("admin-toggle-admin-role").click();
  await expect(page.getByText("Роль администратора сохранена для Managed Player.")).toBeVisible();
  expect(roleBody).toMatchObject({
    is_admin: true,
    note: "Add another platform administrator."
  });

  const userDeletion = page.getByTestId("admin-user-deletion-console");
  await expect(userDeletion).toBeVisible();
  await userDeletion.getByTestId("admin-delete-user-search").fill("managed");
  const managedDeletionRow = userDeletion.locator("tbody tr").filter({ hasText: "managed@example.com" });
  await expect(managedDeletionRow).toBeVisible();
  await managedDeletionRow.click();
  const deleteUserButton = userDeletion.getByTestId("admin-delete-user");
  await expect(deleteUserButton).toBeDisabled();
  await userDeletion.getByTestId("admin-delete-user-confirmation").fill("managed@example.com");
  await userDeletion.getByTestId("admin-delete-user-note").fill("Remove the approved smoke user.");
  await expect(deleteUserButton).toBeEnabled();
  await deleteUserButton.click();
  await expect.poll(() => deleteUserBody).toMatchObject({
    confirmation: "managed@example.com",
    note: "Remove the approved smoke user."
  });
  await expect(userDeletion.getByText("Аккаунт Managed Player удалён из базы данных.")).toBeVisible();
  expect(deleteUserBody).toMatchObject({
    confirmation: "managed@example.com",
    note: "Remove the approved smoke user."
  });

  await page.getByRole("tab", { name: /Preprod QA/ }).click();
  await expect(page.getByText("Дашборд preprod прогонов")).toBeVisible();
  await expect(page.getByTestId("admin-preprod-run-preprod260620120000abcd")).toBeVisible();

  await page.getByRole("tab", { name: /Журнал аудита/ }).click();
  await expect(page.getByText("Что такое аудит")).toBeVisible();
  await expect(
    page.getByTestId("admin-audit-inspector").getByText("Resolve disputed bracket state.", { exact: true })
  ).toBeVisible();

  await page.setViewportSize({ width: 390, height: 1000 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
});

test("tournament scope filter loads private organizer and registered tournaments", async ({ page }) => {
  await page.route("**/api/v1/tournaments/mine?**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const scope = requestUrl.searchParams.get("scope");
    const limit = Number(requestUrl.searchParams.get("limit") ?? 9);
    const offset = Number(requestUrl.searchParams.get("offset") ?? 0);
    const allTournaments = [
      {
        id: "t_private_owned",
        slug: "private-owned-cup",
        name: "Private Owned Cup",
        description: "Owned private tournament",
        visibility: "invite_only",
        status: "registration_open",
        format_slug: "solo",
        organizer_user_id: "u_lisalexy",
        organizer_display_name: "lisalexy",
        participant_count: 4,
        allowed_ranks: ["Initiate", "Seeker"],
        max_participants: 16,
        current_user_participant_status: null,
        current_user_has_invite_access: false,
        starts_at: "2026-06-08T17:00:00Z",
        created_at: new Date().toISOString()
      },
      {
        id: "t_private_registered",
        slug: "private-registered-cup",
        name: "Private Registered Cup",
        description: "Registered private tournament",
        visibility: "invite_only",
        status: "registration_open",
        format_slug: "solo",
        organizer_user_id: "u_other",
        organizer_display_name: "Other Organizer",
        participant_count: 12,
        allowed_ranks: ["Phantom"],
        max_participants: 32,
        current_user_participant_status: "registered",
        current_user_has_invite_access: false,
        starts_at: "2026-06-09T17:00:00Z",
        created_at: new Date().toISOString()
      }
    ];
    const filtered = allTournaments.filter((tournament) => (
      scope === "mine"
        ? tournament.organizer_user_id === "u_lisalexy"
        : scope === "registered"
          ? tournament.current_user_participant_status !== null
          : true
    ));
    const pageItems = filtered.slice(offset, offset + limit);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "X-Total-Count": String(filtered.length),
        "X-Limit": String(limit),
        "X-Offset": String(offset),
        "X-Has-More": String(offset + pageItems.length < filtered.length)
      },
      body: JSON.stringify(pageItems)
    });
  });

  await page.goto("/tournaments");

  await page.getByRole("main").getByTestId("tournament-scope-filter").selectOption("mine");
  await expect(page.getByRole("heading", { name: "Private Owned Cup" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Private Registered Cup" })).toHaveCount(0);

  await page.getByRole("main").getByTestId("tournament-scope-filter").selectOption("registered");
  await expect(page.getByRole("heading", { name: "Private Registered Cup" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Private Owned Cup" })).toHaveCount(0);
});

test("tournament list loads nine cards at a time on scroll and centers the count", async ({ page }) => {
  const requestedOffsets: number[] = [];
  await page.route("**/api/v1/tournaments/mine?**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const limit = Number(requestUrl.searchParams.get("limit") ?? 9);
    const offset = Number(requestUrl.searchParams.get("offset") ?? 0);
    requestedOffsets.push(offset);
    const status = requestUrl.searchParams.get("status");
    const allTournaments = Array.from({ length: 10 }, (_, index) => ({
      id: `t_paged_${index + 1}`,
      slug: `paged-cup-${index + 1}`,
      name: `Paged Cup ${index + 1}`,
      description: "Pagination smoke tournament",
      visibility: "public",
      status: index === 9 ? "registration_closed" : "registration_open",
      format_slug: "solo",
      organizer_user_id: "u_lisalexy",
      organizer_display_name: "lisalexy",
      participant_count: index,
      allowed_ranks: ["Initiate", "Seeker"],
      max_participants: 16,
      current_user_participant_status: null,
      current_user_has_invite_access: false,
      starts_at: `2026-06-${String(index + 10).padStart(2, "0")}T17:00:00Z`,
      created_at: new Date().toISOString()
    }));
    const filtered = status
      ? allTournaments.filter((tournament) => tournament.status === status)
      : allTournaments;
    const pageItems = filtered.slice(offset, offset + limit);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "X-Total-Count": String(filtered.length),
        "X-Limit": String(limit),
        "X-Offset": String(offset),
        "X-Has-More": String(offset + pageItems.length < filtered.length)
      },
      body: JSON.stringify(pageItems)
    });
  });

  await page.goto("/tournaments");
  await page.getByRole("main").getByTestId("tournament-scope-filter").selectOption("mine");

  await page.getByRole("main").getByTestId("status-filter").selectOption("registration_open");
  await expect(page.getByTestId("tournament-card")).toHaveCount(9);
  await expect(page.getByTestId("tournaments-load-more")).toHaveCount(0);
  await expect(page.getByTestId("shown-count")).toHaveText("Показано 9 из 9 турниров");

  await page.getByRole("main").getByTestId("status-filter").selectOption("all");
  await expect(page.getByTestId("tournament-card")).toHaveCount(9);
  await page.getByTestId("tournaments-load-sentinel").scrollIntoViewIfNeeded();
  await expect(page.getByTestId("tournament-card")).toHaveCount(10);
  await expect(page.getByTestId("tournaments-load-more")).toHaveCount(0);
  await expect(page.getByTestId("shown-count")).toHaveText("Показано 10 из 10 турниров");
  expect(requestedOffsets).toContain(9);
  const countBox = await page.getByTestId("shown-count").boundingBox();
  const mainBox = await page.locator("main").boundingBox();
  expect(Math.abs((countBox?.x ?? 0) + (countBox?.width ?? 0) / 2 - ((mainBox?.x ?? 0) + (mainBox?.width ?? 0) / 2))).toBeLessThanOrEqual(1);
});

test("tournament card points to a detail route that renders", async ({ page }) => {
  await page.goto("/tournaments");

  const detailsLink = page.getByRole("link", { name: "Открыть турнир: Night Veil Open #5" });
  await expect(detailsLink).toBeVisible();
  await expect(detailsLink).toHaveAttribute("href", "/tournaments/night-veil-open-5");
  await page.goto("/tournaments/night-veil-open-5");
  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5$/, { timeout: 20_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Night Veil Open #5" })).toBeVisible();
});

test("status badges display labels from status data", async ({ page }) => {
  await page.goto("/tournaments");

  const open = page.getByTestId("tournament-status-badge").filter({ hasText: "Регистрация открыта" }).first();
  const closed = page.getByTestId("tournament-status-badge").filter({ hasText: "Регистрация закрыта" }).first();
  await expect(open).toBeVisible();
  await expect(closed).toBeVisible();
  await expect(open).toHaveCSS("background-color", "rgba(20, 83, 65, 0.58)");
  await expect(closed).toHaveCSS("background-color", "rgba(148, 163, 184, 0.14)");
  await expect(open).toHaveCSS("box-shadow", /rgba\(74, 222, 128, 0\.16\)/);
  await expect(closed).toHaveCSS("box-shadow", /rgba\(226, 232, 240, 0\.11\)/);
  await expect(open).toHaveCSS("position", "absolute");
  for (const badge of [open, closed]) {
    const placement = await badge.evaluate((element) => {
      const badgeBox = element.getBoundingClientRect();
      const bannerBox = element.parentElement!.getBoundingClientRect();
      return {
        top: badgeBox.top - bannerBox.top,
        right: bannerBox.right - badgeBox.right,
        insideLeft: badgeBox.left >= bannerBox.left,
        insideRight: badgeBox.right <= bannerBox.right
      };
    });
    expect(Math.abs(placement.top - 14)).toBeLessThanOrEqual(1);
    expect(Math.abs(placement.right - 14)).toBeLessThanOrEqual(1);
    expect(placement.insideLeft).toBe(true);
    expect(placement.insideRight).toBe(true);
  }

  await page.evaluate(() => {
    for (const [status, label] of [["in-progress", "Идет"], ["completed", "Завершен"], ["cancelled", "Отменен"]]) {
      const badge = document.createElement("span");
      badge.className = `badge status tournament-status-${status}`;
      badge.dataset.testStatus = status;
      badge.textContent = label;
      document.body.append(badge);
    }
  });
  await expect(page.locator('[data-test-status="in-progress"]')).toHaveCSS("background-color", "rgba(30, 82, 139, 0.52)");
  await expect(page.locator('[data-test-status="completed"]')).toHaveCSS("background-color", "rgba(87, 29, 45, 0.58)");
  await expect(page.locator('[data-test-status="cancelled"]')).toHaveCSS("background-color", "rgba(87, 29, 45, 0.58)");
});

test("patch detail keeps objective sections, the upper cover crop, and no source toolbar", async ({ page }) => {
  await page.route("https://assets-bucket.deadlock-api.com/**", async (route) => {
    await route.fulfill({
      body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
      contentType: "image/png",
      status: 200
    });
  });
  await page.goto("/patches/1836506165584438");

  const cover = page.locator(".patch-article-cover");
  await expect(cover).toBeVisible();
  await expect(cover).toHaveCSS("background-position", /50% 0%$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Minor Update - 09-07-2026");
  await expect(page.locator(".hero-subtitle")).toHaveCount(0);
  await expect(page.locator(".patch-article-toolbar")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Открыть источник" })).toHaveCount(0);
  const patchSections = page.locator(".patch-section");
  await expect(patchSections).toHaveCount(5);
  await expect(patchSections.getByRole("heading", { level: 2 })).toHaveText([
    "Общие изменения",
    "Урна",
    "Нестабильный разлом",
    "Crushing Fists",
    "The Doorman"
  ]);
  await expect(patchSections.nth(1)).toHaveClass(/objective-patch-section/u);
  await expect(patchSections.nth(2)).toHaveClass(/objective-patch-section/u);
  await expect(page.locator('[data-objective-icon="source"], [data-objective-icon="fallback"]')).toHaveCount(2);
  await expect(page.locator(".patch-objective-image")).toHaveCount(1);
  await expect(page.locator(".patch-objective-fallback")).toHaveCount(1);
  await expect(page.locator(".patch-objective-image")).toHaveAttribute(
    "src",
    /^https:\/\/assets-bucket\.deadlock-api\.com\//u
  );
  await expect(page.locator(".patch-item-image")).toHaveCount(1);
  await expect(page.locator(".patch-item-image")).toHaveCSS("padding", "0px");
  await expect(page.locator(".patch-item-image")).toHaveCSS("border-width", "0px");
  await expect(page.locator(".patch-item-image")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(page.getByText("Изменения предмета")).toHaveCount(0);
  await expect(page.getByText("Изменения героя")).toHaveCount(0);
  const inlinePatchImage = page.locator(".patch-inline-image img");
  await expect(inlinePatchImage).toHaveCount(1);
  await expect(inlinePatchImage).toHaveAttribute("src", /f6a6d5724077ee5ea7b3b3701f4af907c9517df4\.png/);
  const generalChanges = page.locator(".general-patch-section .patch-change-list > li");
  await expect(generalChanges).toHaveCount(2);
  expect(await generalChanges.nth(0).evaluate((node) => (
    node.nextElementSibling?.classList.contains("patch-inline-image") ?? false
  ))).toBe(true);
  await expectNoHorizontalOverflow(page);
});

test("featured patch shows NEW only during its first three days", async ({ page }) => {
  const now = Date.parse("2026-07-11T12:00:00Z");
  expect(isNewPatch("2026-07-09T12:00:00Z", now)).toBe(true);
  expect(isNewPatch("2026-07-06T12:00:00Z", now)).toBe(false);

  await page.goto("/");
  await expect(page.locator(".patch-new-ribbon")).toHaveCount(0);
});

test("tournament cards keep compact and evenly aligned metadata", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/tournaments");
  await waitForAllTournamentCardAssets(page);
  await page.locator(".hero-wrap").scrollIntoViewIfNeeded();

  const card = page.getByTestId("tournament-card").first();
  await expect(card).toBeVisible();
  await card.hover();
  await expect(card).toHaveCSS("transform", "none");
  const cardBox = await card.boundingBox();
  expect(cardBox).not.toBeNull();
  expect(cardBox!.height).toBeLessThanOrEqual(302);

  const gridBox = await page.getByTestId("tournaments-grid").boundingBox();
  const mainBox = await page.locator("main.main").boundingBox();
  expect(gridBox).not.toBeNull();
  expect(mainBox).not.toBeNull();
  expect(gridBox!.x).toBeGreaterThanOrEqual(mainBox!.x - 1);
  expect(gridBox!.x + gridBox!.width).toBeLessThanOrEqual(mainBox!.x + mainBox!.width + 1);
  expect(await page.getByTestId("tournament-card").evaluateAll((cards) => (
    new Set(cards.slice(0, 3).map((item) => Math.round(item.getBoundingClientRect().top))).size
  ))).toBe(1);
  await expectNoHorizontalOverflow(page);

  const metadataLabelTops = await card.locator(".org-label, .team-limit-label, .participants-top").evaluateAll(
    (elements) => elements.map((element) => element.getBoundingClientRect().top)
  );
  expect(Math.max(...metadataLabelTops) - Math.min(...metadataLabelTops)).toBeLessThanOrEqual(2);

  const gaps = await card.evaluate((element) => {
    const top = element.querySelector(".card-top")!.getBoundingClientRect();
    const ranks = element.querySelector(".ranks-section")!.getBoundingClientRect();
    const metadata = element.querySelector(".tournament-card-meta")!.getBoundingClientRect();
    return {
      topToRanks: ranks.top - top.bottom,
      ranksToMetadata: metadata.top - ranks.bottom
    };
  });
  expect(Math.abs(gaps.topToRanks - gaps.ranksToMetadata)).toBeLessThanOrEqual(2);

  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("tournaments-1920-loaded.png")
    });
  }
});

test("mobile tournament metadata keeps limits together and the organizer last", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile metadata geometry is owned by the phone viewport.");
  await page.setViewportSize({ width: 320, height: 900 });
  await page.route("**/api/v1/tournaments**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET" || !url.searchParams.has("status")) {
      await route.fallback();
      return;
    }
    const response = await route.fetch();
    const tournaments = await response.json();
    if (Array.isArray(tournaments) && tournaments.length > 0) {
      tournaments[0] = {
        ...tournaments[0],
        participant_count: 99999,
        max_participants: 99999,
        teams_count: 8192
      };
    }
    await route.fulfill({ response, json: tournaments });
  });
  await page.goto("/tournaments");
  await page.getByRole("main").getByTestId("status-filter").selectOption("registration_open");

  const card = page.getByTestId("tournament-card").first();
  await expect(card).toBeVisible();
  await expect(card.getByTestId("team-limit-value")).toContainText("8192");
  await expect(card.locator(".participants-value")).toHaveText("99999 / 99999");
  const positions = await card.evaluate((element) => ({
    teams: element.querySelector(".team-limit")!.getBoundingClientRect().top,
    registrations: element.querySelector(".participants")!.getBoundingClientRect().top,
    organizer: element.querySelector(".organizer")!.getBoundingClientRect().top
  }));
  expect(Math.abs(positions.teams - positions.registrations)).toBeLessThanOrEqual(1);
  expect(positions.registrations).toBeLessThan(positions.organizer);
});

test("home uses a balanced hero, numbered workflow, and featured patch hierarchy", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Detailed geometry is owned by the 1920px project.");
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/");

  const featuredPatch = page.locator(".patch-featured");
  const compactPatches = page.locator(".patch-compact-card");
  const videos = page.locator(".video-card");
  await expect(featuredPatch).toHaveCount(1);
  await expect(compactPatches).toHaveCount(3);
  await expect(videos).toHaveCount(4);
  const featuredImage = featuredPatch.locator("img");
  await expect(featuredImage).toHaveAttribute("src", "/assets/preview/patch-featured.webp");
  await expect(featuredImage).toHaveAttribute("loading", "eager");
  await expect(featuredImage).toHaveAttribute("fetchpriority", "high");
  await expect(featuredImage).toHaveAttribute("srcset", /patch-featured-768\.webp 768w/u);
  await expect(compactPatches.first().locator("img")).toHaveAttribute("loading", "lazy");
  await expect(compactPatches.first().locator("img")).toHaveAttribute("srcset", /patch-archive-city-320\.webp 320w/u);
  await expect(page.locator(".home-content-group").first()).toHaveCSS("border-top-width", "0px");
  const heroActions = page.locator(".hero-actions");
  await expect(heroActions.getByRole("link", { name: "Найти турнир" })).toHaveAttribute("href", "/tournaments");
  await expect(heroActions.getByRole("link", { name: "Создать турнир" })).toHaveAttribute("href", "/tournaments/new");
  await expect(heroActions.getByRole("link", { name: "Найти турнир" })).toHaveClass(/hero-action-primary/);
  await expect(heroActions.getByRole("link", { name: "Создать турнир" })).toHaveClass(/hero-action-secondary/);
  const heroBox = await page.locator(".hero-wrap-home").boundingBox();
  const heroActionsBox = await heroActions.boundingBox();
  const heroTitleBox = await page.locator(".hero-wrap-home .hero-title").boundingBox();
  const heroSubtitleBox = await page.locator(".hero-wrap-home .hero-subtitle").boundingBox();
  const firstFlowBox = await page.locator(".home-flow").boundingBox();
  expect(heroBox).not.toBeNull();
  expect(heroActionsBox).not.toBeNull();
  expect(heroTitleBox).not.toBeNull();
  expect(heroSubtitleBox).not.toBeNull();
  expect(firstFlowBox).not.toBeNull();
  const heroRhythm = [
    heroTitleBox!.y - heroBox!.y,
    heroActionsBox!.y - (heroSubtitleBox!.y + heroSubtitleBox!.height),
    heroBox!.y + heroBox!.height - (heroActionsBox!.y + heroActionsBox!.height),
  ];
  expect(heroRhythm.every((gap) => Math.abs(gap - 38) <= 1)).toBe(true);
  expect(Math.abs(firstFlowBox!.y - (heroBox!.y + heroBox!.height) - 38)).toBeLessThanOrEqual(1);
  await expect(page.getByText("КАНАЛ OLD SPARKY", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Последние видео" })).toBeVisible();
  await expect(page.getByText("Открой подходящий турнир и зарегистрируйся", { exact: true })).toBeVisible();
  await expect(page.getByText("Дата и время подтверждения указаны заранее")).toBeVisible();
  await expect(page.getByText("ОФИЦИАЛЬНЫЕ ОБНОВЛЕНИЯ", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Последние патчи Deadlock" })).toBeVisible();
  await expect(page.getByText("СООБЩЕСТВО", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Мы в соцсетях" })).toBeVisible();
  await expect(page.locator(".home-section-heading")).toHaveCount(3);
  await expect(featuredPatch.locator(".patch-title")).toHaveText("Minor Update");
  await expect(featuredPatch.locator(".patch-date")).toHaveText("09-07-2026");
  await expect(featuredPatch).toHaveJSProperty("tagName", "ARTICLE");
  await expect(featuredPatch.getByRole("link", { name: "Читать изменения" })).toHaveAttribute(
    "href",
    "/patches/1836506165584438",
  );
  await expect(compactPatches.first()).toHaveAttribute("href", "/patches/1836506165584431");
  await expect(compactPatches.first().locator(".patch-title")).toHaveText("Minor Update");
  await expect(compactPatches.first().locator(".patch-date")).toHaveText("08-07-2026");
  const compactPatchGeometry = await compactPatches.first().evaluate((card) => {
    const cardBox = card.getBoundingClientRect();
    const artBox = card.querySelector(".patch-compact-art")!.getBoundingClientRect();
    const titleBox = card.querySelector(".patch-title")!.getBoundingClientRect();
    const dateBox = card.querySelector(".patch-date")!.getBoundingClientRect();
    return {
      card: { x: cardBox.x, y: cardBox.y, width: cardBox.width, height: cardBox.height },
      art: { x: artBox.x, y: artBox.y, width: artBox.width, height: artBox.height },
      titleCenter: titleBox.x + titleBox.width / 2,
      cardCenter: cardBox.x + cardBox.width / 2,
      titleLeft: titleBox.x,
      titleBottom: titleBox.bottom,
      dateLeft: dateBox.x,
      dateTop: dateBox.top
    };
  });
  expect(Math.abs(compactPatchGeometry.art.x - compactPatchGeometry.card.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(compactPatchGeometry.art.y - compactPatchGeometry.card.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(compactPatchGeometry.art.width - compactPatchGeometry.card.width)).toBeLessThanOrEqual(2);
  expect(Math.abs(compactPatchGeometry.art.height - compactPatchGeometry.card.height)).toBeLessThanOrEqual(2);
  expect(Math.abs(compactPatchGeometry.titleCenter - compactPatchGeometry.cardCenter)).toBeLessThanOrEqual(1);
  expect(Math.abs(compactPatchGeometry.dateLeft - compactPatchGeometry.titleLeft)).toBeLessThanOrEqual(1);
  expect(compactPatchGeometry.dateTop).toBeGreaterThan(compactPatchGeometry.titleBottom);
  const socialCards = page.locator(".home-social-card");
  await expect(socialCards).toHaveCount(5);
  await expect(page.getByRole("link", { name: /Twitch/ })).toHaveAttribute("href", "https://www.twitch.tv/old_sparky");
  await expect(page.getByRole("link", { name: /YouTube/ })).toHaveAttribute("href", "https://www.youtube.com/@deadlockOldSparky");
  await expect(page.getByRole("link", { name: /Discord/ })).toHaveAttribute("href", "https://discord.com/invite/cWVh7fT");
  await expect(page.getByRole("link", { name: /Telegram/ })).toHaveAttribute("href", "https://t.me/oldsparkydeadlock");
  await expect(page.getByRole("link", { name: /VK/ })).toHaveAttribute("href", "https://vk.ru/osdota");
  expect(await socialCards.evaluateAll((cards) => cards.map((card) => card.getAttribute("aria-label")))).toEqual([
    "YouTube", "Twitch", "Discord", "Telegram", "VK"
  ]);
  await expect(socialCards.locator(":scope > svg")).toHaveCount(0);
  await expect(socialCards.first()).toHaveText("");
  const socialGridBox = await page.locator(".home-socials-grid").boundingBox();
  const homeWorkingAreaBox = await page.locator(".home-flow").boundingBox();
  expect(socialGridBox).not.toBeNull();
  expect(homeWorkingAreaBox).not.toBeNull();
  expect(Math.abs(socialGridBox!.x - homeWorkingAreaBox!.x)).toBeLessThanOrEqual(1);
  expect(
    Math.abs(
      socialGridBox!.x
      + socialGridBox!.width
      - (homeWorkingAreaBox!.x + homeWorkingAreaBox!.width),
    ),
  ).toBeLessThanOrEqual(1);
  const flowCards = page.locator(".home-flow-card");
  await expect(flowCards).toHaveCount(3);
  await expect(page.getByRole("heading", { level: 2, name: "Как проходит турнир" })).toHaveCount(1);
  await expect(flowCards.getByRole("heading", { level: 3 })).toHaveText([
    "Выбери турнир",
    "Подтверди готовность",
    "Попади в команду"
  ]);
  await expect(flowCards.locator("p")).toHaveText([
    "Открой подходящий турнир и зарегистрируйся",
    "Дата и время подтверждения указаны заранее",
    "Команды формируются автоматически"
  ]);
  await expect(page.locator(".home-flow-number")).toHaveText(["01", "02", "03"]);
  await expect(page.locator(".home-flow-arrow")).toHaveCount(2);
  await expect(flowCards.locator("img")).toHaveCount(0);
  await expect(page.locator(".home-flow-icon")).toHaveCount(0);
  const flowCardBox = await flowCards.first().boundingBox();
  expect(flowCardBox).not.toBeNull();
  expect(flowCardBox!.height).toBe(104);
  expect(flowCardBox!.width).toBeGreaterThan(flowCardBox!.height);
  const flowSurfaceBeforeHover = await flowCards.first().evaluate((card) => {
    const style = getComputedStyle(card);
    return {
      backgroundImage: style.backgroundImage,
      borderColor: style.borderColor,
      boxShadow: style.boxShadow,
      transform: style.transform
    };
  });
  await flowCards.first().hover();
  const flowSurfaceAfterHover = await flowCards.first().evaluate((card) => {
    const style = getComputedStyle(card);
    return {
      backgroundImage: style.backgroundImage,
      borderColor: style.borderColor,
      boxShadow: style.boxShadow,
      transform: style.transform
    };
  });
  expect(flowSurfaceAfterHover).toEqual(flowSurfaceBeforeHover);
  const firstVideo = videos.first();
  await expect(firstVideo.locator(".video-play")).toHaveCSS("filter", "none");
  await firstVideo.hover();
  await expect(firstVideo).toHaveCSS("transform", "none");
  await expect(firstVideo.locator(".video-play")).not.toHaveCSS("filter", "none");
  const firstSocial = socialCards.first();
  await expect(firstSocial.locator(".home-social-logo")).toHaveCSS("filter", "none");
  await firstSocial.hover();
  await expect(firstSocial).toHaveCSS("transform", "none");
  await expect(firstSocial.locator(".home-social-logo")).not.toHaveCSS("filter", "none");
  await compactPatches.first().hover();
  await expect(compactPatches.first()).toHaveCSS("transform", "none");
  await expect(compactPatches.first()).toHaveCSS("border-color", "rgba(167, 139, 250, 0.52)");
  const surfaceStyles = await page.evaluate(() => {
    const flow = document.querySelector<HTMLElement>(".home-flow-card")!;
    const flowNumber = flow.querySelector<HTMLElement>(".home-flow-number")!;
    const video = document.querySelector<HTMLElement>(".video-card")!;
    const social = document.querySelector<HTMLElement>(".home-social-card")!;
    return {
      flowBackground: getComputedStyle(flow).backgroundImage,
      flowHighlight: getComputedStyle(flow, "::before").backgroundImage,
      flowShadow: getComputedStyle(flow).boxShadow,
      flowNumberBackground: getComputedStyle(flowNumber).backgroundImage,
      flowNumberBorderWidth: getComputedStyle(flowNumber).borderTopWidth,
      flowNumberShadow: getComputedStyle(flowNumber).boxShadow,
      flowNumberWidth: flowNumber.getBoundingClientRect().width,
      videoBackdrop: getComputedStyle(video).backdropFilter,
      videoShadow: getComputedStyle(video).boxShadow,
      socialBackdrop: getComputedStyle(social).backdropFilter,
      socialTransform: getComputedStyle(social).transform,
    };
  });
  expect(surfaceStyles.flowBackground).toContain("linear-gradient");
  expect(surfaceStyles.flowHighlight).toContain("linear-gradient");
  expect(surfaceStyles.flowShadow).toContain("inset");
  expect(surfaceStyles.flowNumberBackground).toBe("none");
  expect(surfaceStyles.flowNumberBorderWidth).toBe("0px");
  expect(surfaceStyles.flowNumberShadow).toBe("none");
  expect(surfaceStyles.flowNumberWidth).toBe(40);
  expect(surfaceStyles.videoBackdrop).toContain("blur");
  expect(surfaceStyles.videoShadow).toContain("inset");
  expect(surfaceStyles.socialBackdrop).toBe("none");
  expect(surfaceStyles.socialTransform).toBe("none");
  const headingStyles = await page.evaluate(() => {
    const accents = [...document.querySelectorAll<HTMLElement>(".home-section-accent")];
    const arena = getComputedStyle(document.querySelector<HTMLElement>(".brand-sub")!).color;
    return {
      accentColors: accents.map((accent) => getComputedStyle(accent).backgroundColor),
      accentFilters: accents.map((accent) => getComputedStyle(accent).filter),
      accentShadows: accents.map((accent) => getComputedStyle(accent).boxShadow),
      arena,
    };
  });
  expect(headingStyles.accentColors).toEqual([headingStyles.arena, headingStyles.arena, headingStyles.arena]);
  expect(headingStyles.accentFilters).toEqual(["none", "none", "none"]);
  expect(headingStyles.accentShadows).toEqual(["none", "none", "none"]);
  const flowCopyGeometry = await flowCards.first().evaluate((card) => {
    const cardBox = card.getBoundingClientRect();
    const copy = card.querySelector<HTMLElement>(".home-flow-copy")!.getBoundingClientRect();
    const number = card.querySelector<HTMLElement>(".home-flow-number")!.getBoundingClientRect();
    const text = card.querySelector<HTMLElement>(".home-flow-text")!.getBoundingClientRect();
    return {
      copyTop: copy.top - cardBox.top,
      centerDelta: Math.abs((number.top + number.height / 2) - (text.top + text.height / 2))
    };
  });
  expect(flowCopyGeometry.copyTop).toBeLessThanOrEqual(1);
  expect(flowCopyGeometry.centerDelta).toBeLessThanOrEqual(1);
  const homeGroups = await page.locator(".home-flow, .home-content-group, .home-socials").evaluateAll((groups) => (
    groups.map((group) => {
      const box = group.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    })
  ));
  expect(homeGroups).toHaveLength(4);
  expect(homeGroups.every((group) => Math.abs(group.left - homeGroups[0].left) <= 1)).toBe(true);
  expect(homeGroups.every((group) => Math.abs(group.right - homeGroups[0].right) <= 1)).toBe(true);
  const groupGaps = homeGroups.slice(1).map((group, index) => Math.round(group.top - homeGroups[index].bottom));
  expect(new Set(groupGaps).size).toBe(1);
  expect(groupGaps[0]).toBe(38);
  const headingContentGaps = await page.locator(".home-content-group, .home-socials").evaluateAll((groups) => groups.map((group) => {
    const headingElement = group.querySelector<HTMLElement>(".home-section-heading")!;
    const heading = headingElement.getBoundingClientRect();
    const content = headingElement.nextElementSibling!.getBoundingClientRect();
    return Math.round(content.top - heading.bottom);
  }));
  expect(new Set(headingContentGaps)).toEqual(new Set([13]));
  expect(new Set(await videos.evaluateAll((items) => items.map((item) => Math.round(item.getBoundingClientRect().top)))).size).toBe(1);
  const patchGeometry = await page.locator(".patch-showcase").evaluate((showcase) => {
    const whole = showcase.getBoundingClientRect();
    const featured = showcase.querySelector<HTMLElement>(".patch-featured")!.getBoundingClientRect();
    const archive = showcase.querySelector<HTMLElement>(".patch-archive")!.getBoundingClientRect();
    return {
      featuredRatio: featured.width / (featured.width + archive.width),
      withinShowcase: featured.right <= whole.right && archive.right <= whole.right,
    };
  });
  expect(patchGeometry.featuredRatio).toBeGreaterThan(.57);
  expect(patchGeometry.featuredRatio).toBeLessThan(.63);
  expect(patchGeometry.withinShowcase).toBe(true);

  const backgrounds = await page.evaluate(() => ({
    body: getComputedStyle(document.body).backgroundImage,
    hero: getComputedStyle(document.querySelector<HTMLElement>(".hero-wrap-home")!).backgroundImage
  }));
  expect(backgrounds.body.match(/old-sparky-modern-city-v2/gu)?.length).toBe(1);
  expect(backgrounds.hero).not.toContain("old-sparky-modern-city-v2");
  await expectNoHorizontalOverflow(page);

  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("home-1920-loaded.png")
    });
  }
});

test("home hierarchy collapses without overflow on configured viewports", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("article.patch-featured")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  const viewportWidth = page.viewportSize()?.width ?? 1920;
  const flowTops = await page.locator(".home-flow-card").evaluateAll((cards) => (
    cards.map((card) => Math.round(card.getBoundingClientRect().top))
  ));
  const featuredBox = await page.locator("article.patch-featured").boundingBox();
  const archiveBox = await page.locator(".patch-archive").boundingBox();
  expect(featuredBox).not.toBeNull();
  expect(archiveBox).not.toBeNull();

  if (viewportWidth <= 820) {
    expect(new Set(flowTops).size).toBe(3);
    expect(archiveBox!.y).toBeGreaterThan(featuredBox!.y + featuredBox!.height);
    const flowHeights = await page.locator(".home-flow-card").evaluateAll((cards) => (
      cards.map((card) => Math.round(card.getBoundingClientRect().height))
    ));
    expect(new Set(flowHeights)).toEqual(new Set([viewportWidth <= 520 ? 94 : 104]));
  } else {
    expect(new Set(flowTops).size).toBe(1);
    expect(Math.abs(archiveBox!.y - featuredBox!.y)).toBeLessThanOrEqual(1);
  }
});

test("bracket route does not fabricate teams when the API returns an empty bracket", async ({ page }) => {
  await page.goto("/tournaments/citadel-clash-3/bracket");

  await expect(page.getByRole("main").getByTestId("bracket-empty")).toBeVisible();
  await expect(page.getByTestId("bracket-match")).toHaveCount(0);
  await expect(page.getByTestId("bracket-team-name")).toHaveCount(0);
});

test("bracket uses zoom and mouse panning without a technical toolbar", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1920, height: 1080 });
  }
  await page.goto("/tournaments/night-veil-open-5/bracket");

  await expect(page.getByRole("link", { name: "Назад к турниру" })).toHaveAttribute(
    "href",
    "/tournaments/night-veil-open-5"
  );
  await expect(page.locator(".bracket-toolbar")).toHaveCount(0);
  await expect(page.locator(".team-drag-handle")).toHaveCount(0);
  await expect(page.getByText(/Ревизия \d+/)).toHaveCount(0);
  await expect(page.getByText("Зажмите мышь и двигайте сетку.")).toHaveCount(0);
  await expect(page.getByLabel("Управление масштабом сетки")).toBeVisible();
  await expect(page.getByText("R1 M1")).toHaveCount(0);
  await expect(page.getByText("Запланирован", { exact: true })).toHaveCount(0);
  await expect(page.locator(".match-meta")).toHaveText("BO3");
  await expect(page.locator(".match-schedule-controls")).toHaveCount(0);
  await expectSharedBracketTeamTemplate(page);
  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("bracket-guest-1920-loaded.png")
    });
  }

  const shell = page.getByTestId("bracket-shell");
  for (let index = 0; index < 6; index += 1) {
    await page.getByLabel("Приблизить").click();
  }
  await shell.evaluate((node) => node.scrollIntoView({ block: "start" }));
  const box = await shell.boundingBox();
  expect(box).not.toBeNull();
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  await expect.poll(() => shell.evaluate((node) => (
    node.scrollWidth > node.clientWidth || node.scrollHeight > node.clientHeight
  ))).toBe(true);
  const startX = box!.x + box!.width / 2;
  const startY = Math.min(box!.y + box!.height - 30, viewport!.height - 30);
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX - 120, startY - 80, { steps: 12 });
  await page.mouse.up();
  await expect.poll(() => shell.evaluate((node) => node.scrollLeft + node.scrollTop)).toBeGreaterThan(0);
});

test("organizer schedules a match above its teams while score controls stay below", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1920, height: 1080 });
  }
  let schedulePayload: Record<string, unknown> | null = null;
  await page.context().addCookies([{
    name: "bracket-manager-smoke",
    value: "1",
    url: "http://127.0.0.1:3100"
  }]);
  await page.route("**/api/v1/tournaments/bracket-manager-smoke/matches/m_night_final/schedule", async (route) => {
    schedulePayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({})
    });
  });

  await page.goto("/tournaments/bracket-manager-smoke/bracket");
  const dateInput = page.getByRole("main").getByLabel("Дата матча 1-1");
  const timeInput = page.getByRole("main").getByLabel("Время матча 1-1");
  await dateInput.fill("2099-07-26");
  await timeInput.fill("18:00");
  await expect(dateInput).toHaveValue("2099-07-26");
  await expect(timeInput).toHaveValue("18:00");
  await page.getByRole("button", { name: "Сохранить время матча" }).first().click();
  await expect.poll(() => schedulePayload).toMatchObject({
    scheduled_at: "2099-07-26T15:00:00.000Z",
    expected_revision: 0
  });

  const scheduleBox = await page.locator(".match-schedule").first().boundingBox();
  const frameBox = await page.locator(".match-frame").first().boundingBox();
  const scoreBox = await page.locator(".match-controls").first().boundingBox();
  expect((scheduleBox?.y ?? 0) + (scheduleBox?.height ?? 0)).toBeLessThanOrEqual(frameBox?.y ?? 0);
  expect((frameBox?.y ?? 0) + (frameBox?.height ?? 0)).toBeLessThanOrEqual(scoreBox?.y ?? 0);
  await expect(page.getByTestId("bracket-match")).toHaveCount(3);
  await expect(page.getByText(/Победитель четвертьфинала/)).toHaveCount(0);
  await expect(page.getByTestId("bracket-match").nth(2).getByTestId("bracket-team-name")).toHaveText(["", ""]);
  await expect(page.getByTestId("bracket-match").nth(2).locator(".seed, .score")).toHaveText(["", "", "", ""]);
  await expect(page.getByTestId("bracket-match").locator(".emblem")).toHaveCount(0);
  await expectSharedBracketTeamTemplate(page);
  const teamCellAlignment = await page.getByTestId("bracket-match").first().locator(".team-slot-content").first().evaluate((row) => {
    const seed = row.querySelector<HTMLElement>(".seed")!;
    const name = row.querySelector<HTMLElement>(".team-name")!;
    const score = row.querySelector<HTMLElement>(".score")!;
    return {
      seedAlign: getComputedStyle(seed).alignItems,
      seedSelf: getComputedStyle(seed).alignSelf,
      seedJustify: getComputedStyle(seed).justifyContent,
      seedText: getComputedStyle(seed).textAlign,
      nameAlign: getComputedStyle(name).alignItems,
      nameSelf: getComputedStyle(name).alignSelf,
      nameJustify: getComputedStyle(name).justifyContent,
      nameText: getComputedStyle(name).textAlign,
      scoreAlign: getComputedStyle(score).alignItems,
      scoreSelf: getComputedStyle(score).alignSelf,
      scoreJustify: getComputedStyle(score).justifyContent,
      scoreText: getComputedStyle(score).textAlign,
    };
  });
  expect(teamCellAlignment).toMatchObject({
    seedAlign: "center",
    seedSelf: "stretch",
    seedJustify: "flex-start",
    seedText: "left",
    nameAlign: "center",
    nameSelf: "stretch",
    nameJustify: "flex-start",
    nameText: "left",
    scoreAlign: "center",
    scoreSelf: "stretch",
    scoreJustify: "center",
    scoreText: "center",
  });
  const connectorAlignment = await page.evaluate(() => {
    const path = document.querySelector<SVGPathElement>(".bracket-lines path")!;
    const frame = document.querySelector<HTMLElement>("[data-testid='bracket-match'] .match-frame")!;
    const point = path.ownerSVGElement!.createSVGPoint();
    const start = path.getPointAtLength(0);
    point.x = start.x;
    point.y = start.y;
    const screenPoint = point.matrixTransform(path.getScreenCTM()!);
    const frameBox = frame.getBoundingClientRect();
    return Math.abs(screenPoint.y - (frameBox.top + frameBox.height / 2));
  });
  expect(connectorAlignment).toBeLessThanOrEqual(2);
  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("bracket-team-alignment-1920-loaded.png")
    });
  }
});

test("primary navigation links point to Next routes", async ({ page }) => {
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/tournaments", { waitUntil: "domcontentloaded" });

  await page.getByRole("banner").getByRole("link", { name: "Создать турнир", exact: true }).click();
  await expect(page).toHaveURL(/\/tournaments\/new$/);
  await expect(page.getByRole("heading", { name: "Создать турнир" })).toBeVisible();

  await page.goto("/tournaments/night-veil-open-5", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("link", { name: "Перейти к сетке" })).toHaveAttribute("href", "/tournaments/night-veil-open-5/bracket");
  await expect(page.getByRole("main").locator(".bracket-icon path")).toHaveAttribute(
    "d",
    "M10 8h14v12H10zM10 26h14v12H10zM10 44h14v12H10zM24 14h10v18h8M24 32h18M24 50h10V32M42 26h12v12H42z"
  );
  await expect(page.locator(".tournament-description-text")).toHaveCSS("white-space", "pre-line");
  expect(await page.locator(".tournament-description-text").textContent()).toContain("платформы.\nРегистрируйтесь");
});

test("create tournament page hides the form from anonymous users", async ({ page }) => {
  await page.goto("/tournaments/new");

  await expect(page.getByRole("heading", { name: "Войдите или зарегистрируйтесь" })).toBeVisible();
  await expect(page.getByRole("main").getByRole("link", { name: "Войти", exact: true })).toHaveAttribute("href", "/auth/login?returnTo=%2Ftournaments%2Fnew");
  await expect(page.getByRole("main").getByRole("link", { name: "Зарегистрироваться", exact: true })).toHaveAttribute("href", "/auth/register?returnTo=%2Ftournaments%2Fnew");
  await expect(page.locator('form[aria-label="Форма создания турнира"]')).toHaveCount(0);
});

test("create tournament page distinguishes an unavailable session from anonymous access", async ({ page }) => {
  await authenticateTestUser(page, [{ name: "users-me-unavailable-smoke", value: "1" }]);
  await page.goto("/tournaments/new");

  await expect(page.getByRole("heading", { name: "Сессию временно не удалось проверить" })).toBeVisible();
  await expect(page.getByRole("main").getByRole("link", { name: "Войти", exact: true })).toHaveCount(0);
  await expect(page.locator('form[aria-label="Форма создания турнира"]')).toHaveCount(0);
});

test("registration action panel has balanced top and bottom spacing", async ({ page }) => {
  await page.goto("/tournaments/night-veil-open-5");

  const spacing = await page.getByRole("main").locator(".steps-panel").evaluate((panel) => {
    const style = getComputedStyle(panel);
    return {
      top: Number.parseFloat(style.paddingTop),
      bottom: Number.parseFloat(style.paddingBottom)
    };
  });
  expect(spacing.top).toBeGreaterThanOrEqual(16);
  expect(Math.abs(spacing.top - spacing.bottom)).toBeLessThanOrEqual(1);
  await expect(page.getByRole("main").getByTestId("registration-steps")).toContainText("Подтверждение с");
  await expect(page.getByRole("main").getByTestId("registration-steps")).not.toContainText("Подтверждение участия с");
  const actionWeights = await page.getByRole("main").locator(".steps-panel .primary-action, .steps-panel .disabled-action, .steps-panel .status-action").evaluateAll(
    (actions) => actions.map((action) => getComputedStyle(action).fontWeight)
  );
  expect(actionWeights.every((weight) => Number(weight) <= 500)).toBe(true);
  const registrationSteps = page.getByRole("main").getByTestId("registration-steps");
  await expect(registrationSteps.getByText("Авторизуйтесь", { exact: true })).toBeVisible();
  await expect(registrationSteps.getByText("Авторизуйтесь", { exact: true })).toHaveAttribute("aria-disabled", "true");
  await expect(registrationSteps.getByRole("button", { name: "Зарегистрироваться" })).toHaveCount(0);
});

test("create tournament form posts to API and navigates to detail", async ({ page }) => {
  let requestBody: unknown;
  let bannerUploadPath = "";

  await authenticateTestUser(page);

  await page.route("**/api/v1/tournaments**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname.endsWith("/invites/suggest-code")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "SMOKE2026X", available: true })
      });
      return;
    }

    if (url.pathname.endsWith("/invites/code-status")) {
      const code = url.searchParams.get("code") ?? "SMOKE2026X";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code, available: true })
      });
      return;
    }

    if (url.pathname.endsWith("/api/v1/tournaments") && request.method() === "POST") {
      requestBody = JSON.parse(request.postData() ?? "{}");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ slug: "night-veil-open-5" })
      });
      return;
    }

    if (url.pathname.endsWith("/night-veil-open-5/banner") && request.method() === "POST") {
      bannerUploadPath = url.pathname;
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          asset_id: "00000000-0000-4000-8000-000000000099",
          status: "pending",
          status_url: "/api/v1/media/00000000-0000-4000-8000-000000000099/status"
        })
      });
      return;
    }

    await route.fallback();
  });
  await page.route("**/api/v1/media/00000000-0000-4000-8000-000000000099/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        asset_id: "00000000-0000-4000-8000-000000000099",
        purpose: "tournament_banner",
        status: "ready",
        error_code: null,
        variants: [{
          name: "banner-1120",
          width: 1120,
          height: 280,
          byte_size: 2048,
          url: "https://cdn.old-sparky.test/media/banner-1120.webp"
        }]
      })
    });
  });

  await page.goto("/tournaments/new");
  await page.getByRole("main").locator('form[aria-label="Форма создания турнира"]').getByLabel(/Название турнира/).fill("Old Sparky Cup #1");
  await page.getByLabel(/Краткое описание/).fill("Smoke tournament");
  await page.getByLabel("Макс. команд").fill("128");
  const inviteCodeInput = page.getByLabel(/Код приглашения/);
  await inviteCodeInput.clear();
  await inviteCodeInput.fill("SMOKE2026X");
  await expect(inviteCodeInput).toHaveValue("SMOKE2026X");
  await page.getByLabel("Загрузить обложку турнира").setInputFiles({
    name: "banner.webp",
    mimeType: "image/webp",
    buffer: Buffer.from("tournament-banner-smoke")
  });
  await page.getByRole("button", { name: "Создать турнир" }).click();

  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5$/);
  expect(requestBody).toMatchObject({
    name: "Old Sparky Cup #1",
    description: "Smoke tournament",
    cover_url: null,
    max_participants: 999999999,
    teams_count: 128,
    visibility: "invite_only",
    invite_code: "SMOKE2026X",
    format_slug: "solo",
    match_format: "bo1",
    final_format: "bo3",
    allowed_ranks: expect.arrayContaining(["Initiate", "Eternus"])
  });
  expect(bannerUploadPath).toBe("/api/v1/tournaments/night-veil-open-5/banner");
});

test("create tournament form uses organizer defaults and tournament limits", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1920, height: 1080 });
  }
  await authenticateTestUser(page);
  await page.goto("/tournaments/new");

  const serverNowIso = await page.locator(".create-layout").getAttribute("data-server-now");
  expect(serverNowIso).toBeTruthy();
  const expectedDate = new Date(Date.parse(serverNowIso!) + 27 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 10);
  const scheduleDateInputs = page.locator('.schedule-picker[data-picker-type="date"]');
  await expect(scheduleDateInputs).toHaveCount(4);
  for (let index = 0; index < 4; index += 1) {
    await expect(scheduleDateInputs.nth(index)).toHaveAttribute("data-picker-value", expectedDate);
  }
  const firstDateMinimum = await scheduleDateInputs.first().getAttribute("data-picker-min");
  expect(firstDateMinimum).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  await scheduleDateInputs.first().click();
  const calendar = page.getByRole("dialog", { name: "Выбор даты" });
  await expect(calendar).toBeVisible();
  const enabledPastDates = await calendar.locator("button[aria-label]").evaluateAll((buttons, minDate) => (
    buttons.filter((button) => {
      const label = button.getAttribute("aria-label") ?? "";
      const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(label);
      if (!match) {
        return false;
      }
      const iso = `${match[3]}-${match[2]}-${match[1]}`;
      return iso < String(minDate) && !(button as HTMLButtonElement).disabled;
    }).length
  ), firstDateMinimum);
  expect(enabledPastDates).toBe(0);
  const [minimumYear, minimumMonth, minimumDay] = firstDateMinimum!.split("-");
  await calendar.getByRole("button", {
    name: `${minimumDay}.${minimumMonth}.${minimumYear}`
  }).click();

  const firstTimePicker = page.locator('.schedule-picker[data-picker-type="time"]').first();
  const firstTimeMinimum = await firstTimePicker.getAttribute("data-picker-min");
  expect(firstTimeMinimum).toMatch(/^\d{2}:\d{2}$/);
  await firstTimePicker.click();
  const timeOptions = page.getByRole("listbox", { name: "Выбор времени" }).getByRole("option");
  const optionLabels = await timeOptions.allTextContents();
  expect(optionLabels.length).toBeGreaterThan(0);
  expect(optionLabels.every((label) => Number(label.split(":")[1]) % 10 === 0)).toBe(true);
  expect(optionLabels[0] >= firstTimeMinimum!).toBe(true);
  await page.keyboard.press("Escape");

  const titleInput = page.getByRole("main").locator('form[aria-label="Форма создания турнира"]').getByLabel(/Название турнира/);
  await expect(titleInput).toHaveValue("");
  await expect(titleInput).toHaveAttribute("maxlength", "25");
  await expect(titleInput).toHaveAttribute("placeholder", "Например, Old Sparky Cup");
  await titleInput.fill("Кубок Deadlock Cup #1");
  await expect(titleInput).toHaveValue("Deadlock Cup #1");
  await titleInput.fill("Deadlock Championship #1");

  const previewTitle = page.locator(".side-stack .card-title");
  await expect(previewTitle).toHaveText("Deadlock Championship #1");
  await expect(previewTitle).toHaveCSS("overflow", "visible");
  await expect(previewTitle).toHaveCSS("text-overflow", "clip");
  await expect(previewTitle).toHaveCSS("white-space", "normal");
  await expect(previewTitle).toHaveCSS("overflow-wrap", "anywhere");
  await expect(page.getByLabel(/Краткое описание/)).toHaveValue("");
  await expect(page.getByLabel(/Краткое описание/)).toHaveAttribute("placeholder",
    "Трансляция будет на твиче - OldSparky\nДругие соцсети - @OldSparky\nПризовой фонд - 10 денег"
  );
  const descriptionInput = page.getByLabel(/Краткое описание/);
  const longDescriptionLine = "А".repeat(71);
  await descriptionInput.fill(longDescriptionLine);
  await expect(descriptionInput).toHaveValue(`${"А".repeat(70)}\nА`);
  const elevenLines = Array.from({ length: 11 }, (_, index) => `Строка ${index + 1}`).join("\n");
  await descriptionInput.fill(elevenLines);
  await expect(descriptionInput).toHaveValue(Array.from({ length: 10 }, (_, index) => `Строка ${index + 1}`).join("\n"));
  await expect(page.getByLabel("Формат матчей")).toHaveValue("bo1");
  await expect(page.getByText("1. Закрытие регистрации")).toBeVisible();
  await expect(page.getByText("2. Подтверждение участия")).toBeVisible();
  await expect(page.getByText("3. Формирование команд")).toBeVisible();
  await expect(page.getByText("4. Начало турнира")).toBeVisible();
  await expect(page.getByText("Проверяем...")).toHaveCount(0);

  const maxParticipantsInput = page.getByLabel(/Макс. регистраций/);
  await expect(maxParticipantsInput).toHaveAttribute("max", "99999");
  await maxParticipantsInput.fill("1000000000");
  await expect(maxParticipantsInput).toHaveValue("99999");
  await page.getByRole("button", { name: "Без ограничений" }).click();
  await expect(maxParticipantsInput).toHaveValue("");
  const maxTeamsInput = page.getByLabel("Макс. команд");
  await expect(maxTeamsInput).toHaveValue("");
  await expect(page.getByRole("button", { name: "Максимум команд" })).toHaveClass(/active/);
  await page.getByRole("button", { name: "Увеличить количество команд" }).click();
  await expect(maxTeamsInput).toHaveValue("2");
  await maxTeamsInput.fill("129");
  await maxTeamsInput.blur();
  await expect(maxTeamsInput).toHaveValue("256");
  await page.getByRole("button", { name: "Уменьшить количество команд" }).click();
  await expect(maxTeamsInput).toHaveValue("128");
  await page.getByRole("button", { name: "Максимум команд" }).click();
  await expect(maxTeamsInput).toHaveValue("");
  await expect(page.getByRole("button", { name: "Максимум команд" })).toHaveClass(/active/);
  await page.getByRole("button", { name: "Увеличить количество команд" }).click();
  await expect(maxTeamsInput).toHaveValue("2");
  await expect(page.getByRole("button", { name: "Максимум команд" })).not.toHaveClass(/active/);
  await page.getByRole("button", { name: "Максимум команд" }).click();
  await expect(maxTeamsInput).toHaveValue("");
  await expect(page.locator(".side-stack").getByTestId("team-limit-value")).toContainText("8192");
  await expect(page.getByText("Рекомендуемый размер: 1200x240 - JPG, PNG или WebP до 5 МБ")).toBeVisible();
  await expect(page.getByRole("button", { name: "Шаблон 1" })).toHaveClass(/active/);
  const coverPreview = page.locator(".cover-box .cover-preview-image");
  await expect(coverPreview).toHaveAttribute(
    "src",
    /tournament-cover-template-1-v1\.webp\?rev=20260725-2/
  );
  await expect(coverPreview).toHaveCSS("object-fit", "cover");
  await page.getByLabel("Загрузить обложку турнира").setInputFiles({
    name: "oversized-cover.png",
    mimeType: "image/png",
    buffer: Buffer.alloc(5 * 1024 * 1024 + 1)
  });
  await expect(page.getByText("Исходный файл обложки должен быть не больше 5 МБ.")).toBeVisible();
  await page.getByRole("button", { name: "Шаблон 1" }).click();
  await expect(page.getByRole("button", { name: "Шаблон 1" })).toHaveClass(/active/);
  await expect(page.getByText("Рекомендуемый размер: 1200x240 - JPG, PNG или WebP до 5 МБ")).toBeVisible();
  await expect(page.getByText("Выбран готовый шаблон")).toHaveCount(0);
  await expect(page.getByText(/^Выбрано:/)).toHaveCount(0);

  const inviteCodeControl = page.locator(".invite-code-field .inline-group");
  await expect(inviteCodeControl).toHaveCSS("height", "46px");
  await expect(inviteCodeControl).toHaveCSS("gap", "0px");
  await expect(inviteCodeControl).toHaveCSS("border-top-width", "1px");
  await expect(page.locator(".invite-code-actions")).toHaveCSS("grid-template-columns", "46px 46px");
  const inviteCopyButton = page.locator(".invite-code-actions .icon-button").nth(1);
  await expect(inviteCopyButton).toHaveAttribute("aria-label", "Скопировать");
  await inviteCopyButton.click();
  await expect(inviteCopyButton).toHaveAttribute("aria-label", "Скопировано");
  await expect(inviteCopyButton).toBeDisabled();
  await expect(inviteCopyButton.locator(".lucide-check")).toHaveCount(1);
  if (testInfo.project.name === "desktop") {
    await expect(inviteCopyButton).toHaveAttribute("aria-label", "Скопировать", { timeout: 3500 });
    await expect(inviteCopyButton.locator(".lucide-copy")).toHaveCount(1);
  }
  await expect(page.locator(".visibility-segmented")).toHaveCSS("gap", "0px");
  await expect(page.locator(".visibility-segmented")).toHaveCSS("padding", "0px");
  await expect(page.locator(".visibility-segmented")).toHaveCSS("border-top-width", "1px");
  await expect(page.locator(".visibility-segmented .visibility-segment").first()).toHaveCSS("height", "46px");
  await expect(page.locator(".visibility-tooltip")).toContainText("Доступно приватных турниров в этом месяце: 1/1.");
  const teamLimitControl = page.locator(".team-count-field .number-row");
  const registrationLimitControl = page.getByLabel(/Макс. регистраций/).locator("../..");
  await expect(teamLimitControl).toHaveCSS("height", "46px");
  await expect(teamLimitControl).toHaveCSS("gap", "0px");
  await expect(registrationLimitControl).toHaveCSS("height", "46px");
  const infinityAlignment = await teamLimitControl.locator(".infinity-limit-button").evaluate((button) => {
    const buttonBox = button.getBoundingClientRect();
    const contentBox = button.querySelector(".infinity-symbol")!.getBoundingClientRect();
    return {
      horizontal: Math.abs((buttonBox.left + buttonBox.width / 2) - (contentBox.left + contentBox.width / 2)),
      vertical: Math.abs((buttonBox.top + buttonBox.height / 2) - (contentBox.top + contentBox.height / 2))
    };
  });
  expect(infinityAlignment.horizontal).toBeLessThanOrEqual(1);
  expect(infinityAlignment.vertical).toBeLessThanOrEqual(1);
  await maxTeamsInput.focus();
  await expect(maxTeamsInput).toHaveCSS("border-top-width", "1px");
  await expect(maxTeamsInput).toHaveCSS("outline-width", "0px");
  expect(await maxTeamsInput.evaluate((input) => getComputedStyle(input).borderTopColor)).not.toBe("rgba(0, 0, 0, 0)");
  await maxParticipantsInput.focus();
  await expect(maxParticipantsInput).toHaveCSS("border-top-width", "1px");
  await expect(maxParticipantsInput).toHaveCSS("outline-width", "0px");
  const selectedVisibility = page.locator(".visibility-segmented .visibility-segment.active");
  await expect(selectedVisibility).toHaveCount(1);
  const visibilityDivider = page.locator(".visibility-segmented");
  const privateDivider = await visibilityDivider.evaluate((control) => ({
    color: getComputedStyle(control, "::after").backgroundColor,
    width: getComputedStyle(control, "::after").width,
  }));
  await page.getByRole("button", { name: "Публичный турнир" }).click();
  const publicDivider = await visibilityDivider.evaluate((control) => ({
    color: getComputedStyle(control, "::after").backgroundColor,
    width: getComputedStyle(control, "::after").width,
  }));
  expect(publicDivider).toEqual(privateDivider);
  expect(publicDivider.width).toBe("1px");
  await page.getByRole("button", { name: "Приватный", exact: true }).click();
  const selectedControlColors = await page.locator(".cover-template-button.active, .infinity-limit-button.active, .visibility-segment.active").evaluateAll(
    (controls) => controls.map((control) => getComputedStyle(control).backgroundColor)
  );
  expect(new Set(selectedControlColors).size).toBe(1);

  if ((page.viewportSize()?.width ?? 0) > 1660) {
    const mainPanel = await page.locator(".create-main-panel").boundingBox();
    const previewPanel = await page.locator(".create-preview-panel").boundingBox();
    const descriptionBox = await descriptionInput.boundingBox();
    expect(mainPanel).not.toBeNull();
    expect(previewPanel).not.toBeNull();
    expect(descriptionBox).not.toBeNull();
    expect(Math.abs(mainPanel!.y + mainPanel!.height - (previewPanel!.y + previewPanel!.height))).toBeLessThanOrEqual(1);
    expect(descriptionBox!.height).toBeGreaterThan(96);
    const participationPanel = await page.locator(".create-participation-panel").boundingBox();
    const coverPanelBox = await page.locator(".form-stack > article").nth(1).boundingBox();
    const checklistPanel = await page.locator(".organizer-checklist").boundingBox();
    expect(coverPanelBox).not.toBeNull();
    expect(participationPanel).not.toBeNull();
    expect(checklistPanel).not.toBeNull();
    expect(Math.abs(checklistPanel!.y - coverPanelBox!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(Math.round(
      participationPanel!.y - (coverPanelBox!.y + coverPanelBox!.height)
    ))).toBe(18);
    expect(Math.abs(Math.round(
      checklistPanel!.height
      - (participationPanel!.y + participationPanel!.height - coverPanelBox!.y)
    ))).toBeLessThanOrEqual(2);
    expect(Math.abs(Math.round(
      participationPanel!.y + participationPanel!.height
      - (checklistPanel!.y + checklistPanel!.height)
    ))).toBeLessThanOrEqual(2);

    for (const width of [1680, 1920]) {
      await page.setViewportSize({ width, height: 1080 });
      const resizedParticipationPanel = await page.locator(".create-participation-panel").boundingBox();
      const resizedChecklistPanel = await page.locator(".organizer-checklist").boundingBox();
      expect(Math.abs(Math.round(
        resizedParticipationPanel!.y + resizedParticipationPanel!.height
        - (resizedChecklistPanel!.y + resizedChecklistPanel!.height)
      ))).toBeLessThanOrEqual(2);
    }
  }

  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("create-tournament-1920-loaded.png")
    });
  }

  const allRanksButton = page.getByRole("button", { name: "Все ранги" });
  await expect(allRanksButton).toHaveClass(/active/);
  await page.getByRole("button", { name: "Eternus" }).click();
  await expect(allRanksButton).not.toHaveClass(/active/);
});

test("mobile tournament creation ends with checklist, preview, and submit panel", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile creation order and compact schedule are covered once.");
  await authenticateTestUser(page);
  await page.goto("/tournaments/new");

  const checklist = page.locator(".organizer-checklist");
  const preview = page.locator(".create-preview-panel");
  const submitPanel = page.locator(".create-submit-panel");
  const order = await page.locator(".create-layout").evaluate((layout) => {
    const checklistNode = layout.querySelector(".organizer-checklist")!;
    const previewNode = layout.querySelector(".create-preview-panel")!;
    const submitNode = layout.querySelector(".create-submit-panel")!;
    return {
      checklistBeforePreview: Boolean(checklistNode.compareDocumentPosition(previewNode) & Node.DOCUMENT_POSITION_FOLLOWING),
      previewBeforeSubmit: Boolean(previewNode.compareDocumentPosition(submitNode) & Node.DOCUMENT_POSITION_FOLLOWING)
    };
  });
  expect(order).toEqual({ checklistBeforePreview: true, previewBeforeSubmit: true });

  const checklistBox = await checklist.boundingBox();
  const previewBox = await preview.boundingBox();
  const submitBox = await submitPanel.boundingBox();
  expect(checklistBox).not.toBeNull();
  expect(previewBox).not.toBeNull();
  expect(submitBox).not.toBeNull();
  expect(checklistBox!.y + checklistBox!.height).toBeLessThan(previewBox!.y);
  expect(previewBox!.y + previewBox!.height).toBeLessThan(submitBox!.y);
  await expect(submitPanel.getByRole("button", { name: "Создать турнир" })).toHaveAttribute("form", "create-tournament-form");

  const firstSchedule = page.locator(".schedule-field").first();
  const timePicker = firstSchedule.locator('.schedule-picker[data-picker-type="time"]');
  const timezone = firstSchedule.locator(".tz-button");
  const timeBox = await timePicker.boundingBox();
  const timezoneBox = await timezone.boundingBox();
  expect(timeBox).not.toBeNull();
  expect(timezoneBox).not.toBeNull();
  expect(timezoneBox!.x).toBeGreaterThan(timeBox!.x + timeBox!.width);
  expect(Math.abs(timezoneBox!.y - timeBox!.y)).toBeLessThanOrEqual(2);
  expect(timezoneBox!.height).toBeLessThanOrEqual(40);

  await page.getByLabel(/Макс\. регистраций/).fill("99999");
  await page.getByRole("button", { name: "Максимум команд" }).click();
  await expect(preview.getByTestId("team-limit-value")).toContainText("8192");
  await expect(preview.locator(".participants-value")).toHaveText("0 / 99999");
  const limits = await preview.locator(".tournament-card-meta").evaluate((row) => {
    const teams = row.querySelector(".team-limit")!.getBoundingClientRect();
    const participants = row.querySelector(".participants")!.getBoundingClientRect();
    const rowBox = row.getBoundingClientRect();
    return {
      sameRow: Math.abs(teams.top - participants.top),
      teamsInside: teams.right <= rowBox.right + 1,
      participantsInside: participants.right <= rowBox.right + 1
    };
  });
  expect(limits).toEqual({ sameRow: 0, teamsInside: true, participantsInside: true });
  await expectNoHorizontalOverflow(page);
});

test("locked public visibility keeps both tournament visibility buttons aligned", async ({ page }) => {
  await authenticateTestUser(page, [{ name: "limited-creator-smoke", value: "1" }]);
  await page.goto("/tournaments/new");

  const publicButton = page.getByRole("button", { name: "Публичный турнир заблокирован" });
  const privateButton = page.getByRole("button", { name: "Приватный", exact: true });
  await expect(publicButton).toBeDisabled();
  await expect(publicButton.locator("svg")).toHaveCSS("position", "absolute");
  const publicBox = await publicButton.boundingBox();
  const privateBox = await privateButton.boundingBox();
  expect(publicBox).not.toBeNull();
  expect(privateBox).not.toBeNull();
  expect(publicBox!.width).toBe(privateBox!.width);
  expect(publicBox!.height).toBe(privateBox!.height);
});

test("private tournament creation is disabled when the allowance is exhausted", async ({ page }) => {
  await authenticateTestUser(page, [{ name: "private-allowance-exhausted-smoke", value: "1" }]);
  await page.goto("/tournaments/new");

  const privateButton = page.getByRole("button", { name: "Приватный турнир недоступен" });
  await expect(privateButton).toBeDisabled();
  await expect(page.getByRole("button", { name: "Создать турнир" })).toBeDisabled();
  await page.getByRole("button", { name: "Информация о доступе к турнирам" }).click();
  await expect(page.getByText("Доступно приватных турниров в этом месяце: 0/1.")).toBeVisible();
});

test("create tournament rejects an invite code shorter than the API contract before submit", async ({ page }) => {
  await authenticateTestUser(page);
  let requestCount = 0;
  await page.route("**/api/v1/tournaments", async (route) => {
    requestCount += 1;
    await route.fulfill({ status: 500, body: "unexpected request" });
  });

  await page.goto("/tournaments/new");
  await page.getByRole("main").locator('form[aria-label="Форма создания турнира"]').getByLabel(/Название турнира/).fill("Invite Contract Test");
  await page.getByLabel(/Краткое описание/).fill("Contract validation");
  await page.getByLabel("Макс. команд").fill("128");
  await page.getByLabel(/Код приглашения/).fill("SHORT1234");
  await page.getByRole("button", { name: "Создать турнир" }).click();

  await expect(page.getByText("Код должен содержать минимум 10 букв или цифр.")).toBeVisible();
  expect(requestCount).toBe(0);
});

test("create tournament form validation blocks invalid payload before API", async ({ page }) => {
  let requestCount = 0;

  await authenticateTestUser(page);

  await page.route("**/api/v1/tournaments", async (route) => {
    requestCount += 1;
    await route.fulfill({ status: 500, body: "unexpected request" });
  });

  await page.goto("/tournaments/new");
  await page.getByRole("main").locator('form[aria-label="Форма создания турнира"]').getByLabel(/Название турнира/).fill("");
  await page.getByRole("button", { name: "Создать турнир" }).click();

  await expect(page.getByText("Проверьте название, код приглашения, расписание и допустимые ранги.")).toBeVisible();
  expect(requestCount).toBe(0);
});

test("auth forms call platform auth endpoints and preserve web sessions", async ({ page }) => {
  const calls: string[] = [];
  const requestOrder: string[] = [];
  const csrfHeaders: string[] = [];
  const profileCsrfHeaders: string[] = [];
  const profileCookieHeaders: string[] = [];
  const logoutCookieHeaders: string[] = [];
  const authPayloads: Array<Record<string, unknown>> = [];
  let csrfIssueCount = 0;
  let logoutAttemptCount = 0;
  let profileMutationAttemptCount = 0;
  const issuedRegisterToken = `issued-register.${"r".repeat(64)}`;
  const issuedLoginToken = `issued-login.${"l".repeat(64)}`;
  const otherTabRotatedToken = `other-tab.${"t".repeat(64)}`;
  const refreshedToken = `refreshed.${"c".repeat(64)}`;
  const retryIssuedToken = `retry-issued.${"n".repeat(64)}`;

  await page.route("**/api/v1/auth/csrf", async (route) => {
    csrfIssueCount += 1;
    requestOrder.push("csrf");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "cache-control": "no-store",
        "Set-Cookie": `deadlock_platform_session_csrf=${refreshedToken}; Path=/; SameSite=Lax`
      },
      body: JSON.stringify({ csrf_token: refreshedToken })
    });
  });

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.route("**/api/v1/auth/register", async (route) => {
    expectOriginOnlyAuthRequest(route);
    requestOrder.push("register");
    csrfHeaders.push(route.request().headers()["x-csrf-token"] ?? "");
    authPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    calls.push("register");
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      headers: {
        "Set-Cookie": `deadlock_platform_session_csrf=${issuedRegisterToken}; Path=/; SameSite=Lax`,
        "X-CSRF-Token": issuedRegisterToken
      },
      body: JSON.stringify({
        user: {
          id: "u_smoke",
          email: "smoke@example.test",
          display_name: "Smoke Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });

  await page.route("**/api/v1/auth/login", async (route) => {
    expectOriginOnlyAuthRequest(route);
    requestOrder.push("login");
    calls.push("login");
    csrfHeaders.push(route.request().headers()["x-csrf-token"] ?? "");
    authPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "Set-Cookie": `deadlock_platform_session_csrf=${issuedLoginToken}; Path=/; SameSite=Lax`,
        "X-CSRF-Token": issuedLoginToken
      },
      body: JSON.stringify({
        user: {
          id: "u_smoke",
          email: "smoke@example.test",
          display_name: "Smoke Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });

  await page.route("**/api/v1/auth/logout", async (route) => {
    logoutAttemptCount += 1;
    const csrfHeader = route.request().headers()["x-csrf-token"] ?? "";
    logoutCookieHeaders.push(route.request().headers().cookie ?? "");
    csrfHeaders.push(csrfHeader);
    requestOrder.push(`logout:${csrfHeader}`);
    calls.push("logout");
    await route.fulfill({ status: 204 });
  });

  await page.route("**/api/v1/profiles/me", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.fallback();
      return;
    }
    profileMutationAttemptCount += 1;
    const csrfHeader = route.request().headers()["x-csrf-token"] ?? "";
    profileCsrfHeaders.push(csrfHeader);
    profileCookieHeaders.push(route.request().headers().cookie ?? "");
    requestOrder.push(`profile:${csrfHeader}`);
    if (profileMutationAttemptCount === 1) {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        headers: { "cache-control": "no-store" },
        body: JSON.stringify({ detail: "CSRF token is missing or invalid." })
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "Set-Cookie": `deadlock_platform_session_csrf=${retryIssuedToken}; Path=/; SameSite=Lax`,
        "X-CSRF-Token": retryIssuedToken
      },
      body: JSON.stringify({
        user_id: "u_smoke",
        account_email: "smoke@example.test",
        display_name: "RotatedPlayer",
        handle: "rotated-player",
        contact_email: "smoke@example.test",
        roles: ["player"]
      })
    });
  });

  await page.goto("/auth/register");
  await page.getByLabel("Отображаемое имя").fill("Smoke Player");
  await page.getByLabel("Email").fill("smoke@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  await page.getByRole("button", { name: "Создать аккаунт", exact: true }).click();
  await expect(page).toHaveURL(/\/profile\/me$/);
  expect(calls).toContain("register");

  await page.goto("/auth/login?returnTo=%2Ftournaments");
  await page.getByLabel("Email").fill("smoke@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/tournaments$/);
  await expect(page.getByRole("link", { name: /Smoke Player/ })).toBeVisible();
  await expect(page.getByRole("banner").getByRole("button", { name: "Выйти" })).toHaveCount(0);
  await page.getByRole("link", { name: /Smoke Player/ }).click();
  await expect(page.locator(".profile-summary").getByRole("button", { name: /Выйти/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Аккаунт" }).click();
  await expect(page.getByTestId("profile-logout-button")).toHaveText("Выйти из аккаунта");
  await page.getByTestId("profile-logout-button").click();
  await expect(page).toHaveURL(/\/$/u);
  expect(csrfIssueCount).toBe(0);

  await page.goto("/auth/login?returnTo=%2Ftournaments");
  await page.getByLabel("Email").fill("smoke@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page).toHaveURL(/\/tournaments$/);
  const otherTab = await page.context().newPage();
  await otherTab.route("**/api/v1/auth/login", async (route) => {
    expectOriginOnlyAuthRequest(route);
    requestOrder.push("login-other-tab");
    calls.push("login");
    csrfHeaders.push(route.request().headers()["x-csrf-token"] ?? "");
    authPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "Set-Cookie": `deadlock_platform_session_csrf=${otherTabRotatedToken}; Path=/; SameSite=Lax`,
        "X-CSRF-Token": otherTabRotatedToken
      },
      body: JSON.stringify({
        user: {
          id: "u_smoke",
          email: "smoke@example.test",
          display_name: "Smoke Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });
  await otherTab.goto("/auth/login");
  await otherTab.getByLabel("Email").fill("smoke@example.test");
  await otherTab.getByLabel("Пароль").fill("long-password");
  await otherTab.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(otherTab).toHaveURL(/\/$/u);
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "rotated-smoke-session",
    url: "http://127.0.0.1:3100"
  }]);
  await otherTab.close();
  await page.getByRole("link", { name: /Smoke Player/ }).click();
  await page.getByRole("button", { name: "Аккаунт" }).click();
  await page.getByLabel("Ник").fill("RotatedPlayer");
  await page.getByTestId("profile-save-account-button").click();
  await expect.poll(() => profileMutationAttemptCount).toBe(2);
  await page.getByTestId("profile-logout-button").click();
  await expect(page).toHaveURL(/\/$/u);

  await expect.poll(() => calls.filter((call) => call === "logout")).toHaveLength(2);
  expect(csrfHeaders).toEqual([
    "",
    "",
    issuedLoginToken,
    "",
    "",
    retryIssuedToken
  ]);
  expect(profileCsrfHeaders).toEqual([issuedLoginToken, refreshedToken]);
  expect(profileCookieHeaders[0]).toContain(`deadlock_platform_session_csrf=${otherTabRotatedToken}`);
  expect(profileCookieHeaders[1]).toContain(`deadlock_platform_session_csrf=${refreshedToken}`);
  expect(logoutCookieHeaders[0]).toContain(`deadlock_platform_session_csrf=${issuedLoginToken}`);
  expect(logoutCookieHeaders[1]).toContain(`deadlock_platform_session_csrf=${retryIssuedToken}`);
  expect(csrfIssueCount).toBe(1);
  expect(logoutAttemptCount).toBe(2);
  expect(profileMutationAttemptCount).toBe(2);
  expect(requestOrder).toEqual([
    "register",
    "login",
    `logout:${issuedLoginToken}`,
    "login",
    "login-other-tab",
    `profile:${issuedLoginToken}`,
    "csrf",
    `profile:${refreshedToken}`,
    `logout:${retryIssuedToken}`
  ]);
  expect(authPayloads.every((payload) => !("turnstile_token" in payload))).toBe(true);
});

test("auth forms use runtime Turnstile actions and reset a consumed token after failure", async ({ page }) => {
  const loginPayloads: Array<Record<string, unknown>> = [];
  const csrfTokenRequests = await trackCsrfTokenRequests(page);

  await page.route("**/api/v1/auth/security-config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        public_registration_enabled: true,
        email_verification_required: true,
        turnstile_mode: "always",
        turnstile_site_key: "turnstile-smoke-site-key"
      })
    });
  });
  await page.route("**/turnstile/v0/api.js*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `(() => {
        let currentOptions = null;
        window.__turnstileResetCount = 0;
        window.turnstile = {
          render(container, options) {
            currentOptions = options;
            container.dataset.turnstileAction = options.action;
            queueMicrotask(() => options.callback("turnstile-login-initial-token"));
            return "auth-widget";
          },
          remove() {},
          reset() {
            window.__turnstileResetCount += 1;
            queueMicrotask(() => currentOptions.callback("turnstile-login-reset-token"));
          }
        };
      })();`
    });
  });
  await page.route("**/api/v1/auth/login", async (route) => {
    expectOriginOnlyAuthRequest(route);
    loginPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    if (loginPayloads.length === 1) {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Human verification failed." })
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_turnstile",
          email: "turnstile@example.test",
          display_name: "Turnstile Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });

  await page.goto("/auth/register");
  await expect(page.locator(".auth-turnstile-frame")).toHaveAttribute("data-turnstile-action", "register");
  await expect(page.getByText("Защита формы")).toBeVisible();
  await expect(page.locator(".auth-turnstile")).toHaveCSS("border-top-style", "solid");
  await expect(page.locator(".auth-turnstile-heading svg")).toHaveCount(1);
  await expectNoHorizontalOverflow(page);

  await page.goto("/auth/login");
  await expect(page.locator(".auth-turnstile-frame")).toHaveAttribute("data-turnstile-action", "login");
  await page.getByLabel("Email").fill("turnstile@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  const submitButton = page.getByRole("button", { name: "Войти", exact: true });
  await expect(submitButton).toBeEnabled();
  await submitButton.click();
  await expect(page.getByText("Проверка безопасности не пройдена. Выполните её ещё раз.")).toBeVisible();
  await expect.poll(() => page.evaluate(() => (
    Number((window as typeof window & { __turnstileResetCount?: number }).__turnstileResetCount ?? 0)
  ))).toBe(1);
  await expect(submitButton).toBeEnabled();
  await submitButton.click();
  await expect(page).toHaveURL(/\/$/u);

  expect(loginPayloads).toHaveLength(2);
  expect(loginPayloads[0].turnstile_token).toBe("turnstile-login-initial-token");
  expect(loginPayloads[1].turnstile_token).toBe("turnstile-login-reset-token");
  expect(csrfTokenRequests).toEqual([]);
});

test("password reset uses six-digit codes and creates a session", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-08-20T12:00:00Z") });
  const resetPayloads: Array<Record<string, unknown>> = [];
  const csrfTokenRequests = await trackCsrfTokenRequests(page);

  await page.route("**/api/v1/auth/password-reset/request", async (route) => {
    expectOriginOnlyAuthRequest(route);
    resetPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ accepted: true }) });
  });
  await page.route("**/api/v1/auth/password-reset/verify-code", async (route) => {
    expectOriginOnlyAuthRequest(route);
    resetPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ accepted: true }) });
  });

  await page.route("**/api/v1/auth/password-reset/confirm", async (route) => {
    expectOriginOnlyAuthRequest(route);
    resetPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_reset",
          email: "reset@example.test",
          display_name: "Reset Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });

  await page.goto("/reset-password");
  const resetEmail = page.getByLabel("Email");
  await expect(resetEmail).toHaveAttribute("name", "email");
  await expect(resetEmail).toHaveAttribute("autocomplete", "email");
  await resetEmail.fill("reset@example.test");
  await page.getByRole("button", { name: "Отправить код" }).click();
  const resetCode = page.getByLabel("Код подтверждения");
  await expect(resetCode).toHaveAttribute("name", "code");
  await expect(resetCode).toHaveAttribute("autocomplete", "one-time-code");
  await page.clock.fastForward(60_100);
  const resendReset = page.getByRole("button", { name: "Отправить ещё раз", exact: true });
  await expect(resendReset).toBeEnabled();
  await resendReset.click();
  await expect.poll(() => resetPayloads).toHaveLength(2);
  await resetCode.fill("123456");
  await page.getByRole("button", { name: "ОК" }).click();
  const resetNewPassword = page.getByLabel("Новый пароль", { exact: true });
  const resetConfirmPassword = page.getByLabel("Повторите новый пароль");
  await expect(resetNewPassword).toHaveAttribute("name", "new_password");
  await expect(resetConfirmPassword).toHaveAttribute("name", "confirm_password");
  await expect(resetNewPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(resetConfirmPassword).toHaveAttribute("autocomplete", "new-password");
  await resetNewPassword.fill("New-password-42");
  await expect(resetConfirmPassword).toHaveValue("");
  await resetConfirmPassword.fill("New-password-42");
  await page.getByRole("button", { name: "Сменить пароль" }).click();
  await expect(page).toHaveURL(/\/profile\/me$/u);
  expect(resetPayloads).toEqual([
    { email: "reset@example.test" },
    { email: "reset@example.test" },
    { email: "reset@example.test", code: "123456" },
    { email: "reset@example.test", code: "123456", new_password: "New-password-42" }
  ]);
  expect(csrfTokenRequests).toEqual([]);

  await expectNoHorizontalOverflow(page);
});

test("password reset uses one Turnstile challenge before code entry", async ({ page }) => {
  const actions: string[] = [];
  const requestBodies: Array<Record<string, unknown>> = [];
  const csrfTokenRequests = await trackCsrfTokenRequests(page);

  await page.route("**/api/v1/auth/security-config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        public_registration_enabled: true,
        email_verification_required: true,
        turnstile_mode: "always",
        turnstile_site_key: "turnstile-lifecycle-key"
      })
    });
  });
  await page.route("**/turnstile/v0/api.js*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `window.turnstile = {
        render(container, options) {
          container.dataset.turnstileAction = options.action;
          queueMicrotask(() => options.callback("token-" + options.action));
          return options.action;
        },
        remove() {},
        reset() {}
      };`
    });
  });
  await page.route("**/api/v1/auth/password-reset/request", async (route) => {
    expectOriginOnlyAuthRequest(route);
    requestBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ accepted: true }) });
  });
  await page.route("**/api/v1/auth/password-reset/verify-code", async (route) => {
    expectOriginOnlyAuthRequest(route);
    requestBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ accepted: true }) });
  });
  await page.goto("/reset-password");
  await expect(page.locator(".auth-turnstile-frame")).toHaveAttribute("data-turnstile-action", "reset_request");
  actions.push(await page.locator(".auth-turnstile-frame").getAttribute("data-turnstile-action") ?? "");
  await page.getByLabel("Email").fill("unknown@example.test");
  await page.getByRole("button", { name: "Отправить код" }).click();
  await expect(page.locator(".auth-turnstile")).toHaveCount(0);
  await page.getByLabel("Код подтверждения").fill("123456");
  await page.getByRole("button", { name: "ОК" }).click();
  await expect(page.locator(".auth-turnstile")).toHaveCount(0);

  expect(actions).toEqual(["reset_request"]);
  await expect.poll(() => requestBodies).toEqual([
    { email: "unknown@example.test", turnstile_token: "token-reset_request" },
    { email: "unknown@example.test", code: "123456" }
  ]);
  expect(csrfTokenRequests).toEqual([]);
});

test("registration requiring email verification changes inline to a code form", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-08-13T12:00:00Z") });
  await authenticateTestUser(page);
  const csrfTokenRequests = await trackCsrfTokenRequests(page);
  let verificationPayload: Record<string, unknown> | null = null;
  let verificationResendPayload: Record<string, unknown> | null = null;
  let releaseVerification: () => void = () => undefined;
  const verificationGate = new Promise<void>((resolve) => {
    releaseVerification = resolve;
  });
  await page.route("**/api/v1/auth/security-config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        public_registration_enabled: true,
        email_verification_required: true,
        turnstile_mode: "always",
        turnstile_site_key: "turnstile-registration-key"
      })
    });
  });
  await page.route("**/turnstile/v0/api.js*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `window.turnstile = {
        render(container, options) {
          container.dataset.turnstileAction = options.action;
          queueMicrotask(() => options.callback("token-register"));
          return "registration-widget";
        },
        remove() {},
        reset() {}
      };`
    });
  });
  await page.route("**/api/v1/auth/register", async (route) => {
    expectOriginOnlyAuthRequest(route);
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_pending",
          email: "pending@example.test",
          display_name: "Pending Player",
          status: "pending_verification",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false
        },
        expires_at: null,
        verification_required: true
      })
    });
  });
  await page.route("**/api/v1/auth/email-verification/confirm", async (route) => {
    expectOriginOnlyAuthRequest(route);
    verificationPayload = route.request().postDataJSON() as Record<string, unknown>;
    await verificationGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_pending",
          email: "pending@example.test",
          display_name: "Pending Player",
          status: "active",
          created_at: "2026-05-20T00:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          has_password: true
        },
        expires_at: "2026-05-21T00:00:00Z"
      })
    });
  });
  await page.route("**/api/v1/auth/email-verification/resend", async (route) => {
    expectOriginOnlyAuthRequest(route);
    verificationResendPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });

  await page.goto("/auth/register");
  await expect(page.getByRole("banner").getByRole("link", { name: /lisalexy/u })).toBeVisible();
  await expect(page.locator(".auth-turnstile-frame")).toHaveAttribute("data-turnstile-action", "register");
  await page.getByLabel("Отображаемое имя").fill("Pending Player");
  await page.getByLabel("Email").fill("pending@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  await page.getByRole("button", { name: "Создать аккаунт", exact: true }).click();
  await expect(page).toHaveURL(/\/auth\/register$/u);
  await expect(page.getByText(/Введите его в течение 10 минут/)).toBeVisible();
  await expect(page.getByRole("banner").getByRole("link", { name: /lisalexy/u })).toHaveCount(0);
  await expect(page.getByRole("banner").getByRole("link", { name: "Войти", exact: true })).toBeVisible();
  await expect(page.locator(".auth-turnstile")).toHaveCount(0);
  await page.clock.fastForward(60_100);
  const resendVerification = page.getByRole("button", { name: "Отправить ещё раз", exact: true });
  await expect(resendVerification).toBeEnabled();
  await resendVerification.click();
  await expect.poll(() => verificationResendPayload).toEqual({ email: "pending@example.test" });
  await page.getByLabel("Код подтверждения").fill("234567");
  const confirmClick = page.getByRole("button", { name: "ОК" }).click();
  await expect.poll(() => verificationPayload).toEqual({ email: "pending@example.test", code: "234567" });
  await expect(page.getByRole("button", { name: /^Отправить ещё раз/u })).toBeDisabled();
  await expect(page.getByLabel("Код подтверждения")).toBeDisabled();
  releaseVerification();
  await confirmClick;
  await expect(page).toHaveURL(/\/profile\/me$/u);
  expect(csrfTokenRequests).toEqual([]);
});

test("Steam-only account is immediately usable and attaches optional email only after code confirmation", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-08-13T12:00:00Z") });
  await authenticateTestUser(page, [{ name: "steam-only-smoke", value: "1" }]);
  const emailLinkPayloads: Array<Record<string, unknown>> = [];
  let releaseEmailConfirmation: () => void = () => undefined;
  const emailConfirmationGate = new Promise<void>((resolve) => {
    releaseEmailConfirmation = resolve;
  });
  for (const path of ["request", "resend", "confirm"] as const) {
    await page.route(`**/api/v1/auth/email-link/${path}`, async (route) => {
      emailLinkPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
      if (path === "confirm") {
        await emailConfirmationGate;
      }
      await route.fallback();
    });
  }

  await page.goto("/profile/me?tab=account");
  await expect(page.getByRole("link", { name: "Профиль: SteamPlayer" })).toBeVisible();
  await expect(page.getByText("76561198999999999", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Привязать Steam" })).toHaveCount(0);
  await expect(page.getByText(/Почта не обязательна для профиля и участия в турнирах/u)).toBeVisible();

  const emailInput = page.getByTestId("profile-account-email");
  const profileSaveButton = page.getByTestId("profile-save-account-button");
  await expect(emailInput).toHaveValue("");
  await emailInput.fill("linked@example.test");
  await expect(profileSaveButton).toBeEnabled();
  await profileSaveButton.click();
  await expect(page.getByText("Код отправлен на linked@example.test")).toBeVisible();
  const resend = page.getByRole("button", { name: /Отправить ещё раз/u });
  await expect(resend).toBeDisabled();
  await expect(resend).toHaveText(/60с/u);
  await page.clock.fastForward(60_100);
  await expect(resend).toBeEnabled();
  await resend.click();
  await expect(resend).toBeDisabled();
  await page.clock.fastForward(60_100);
  await expect(resend).toBeEnabled();

  await page.getByLabel("Код подтверждения").fill("654321");
  const emailConfirmClick = page.getByRole("button", { name: "Подтвердить", exact: true }).click();
  await expect.poll(() => emailLinkPayloads).toHaveLength(3);
  await expect(resend).toBeDisabled();
  await expect(page.getByLabel("Код подтверждения")).toBeDisabled();
  releaseEmailConfirmation();
  await emailConfirmClick;
  await expect(page.getByText("linked@example.test", { exact: true })).toBeVisible();
  const setPasswordLink = page.getByRole("link", { name: "Установить пароль" });
  await expect(setPasswordLink).toBeVisible();
  expect(emailLinkPayloads).toEqual([
    { email: "linked@example.test" },
    { email: "linked@example.test" },
    { email: "linked@example.test", code: "654321" }
  ]);

  const firstPasswordPayloads: Array<{ path: string; body: Record<string, unknown> }> = [];
  await page.route("**/api/v1/auth/password-reset/request", async (route) => {
    expectOriginOnlyAuthRequest(route);
    firstPasswordPayloads.push({
      path: "/auth/password-reset/request",
      body: route.request().postDataJSON() as Record<string, unknown>
    });
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });
  await page.route("**/api/v1/auth/password-reset/verify-code", async (route) => {
    expectOriginOnlyAuthRequest(route);
    firstPasswordPayloads.push({
      path: "/auth/password-reset/verify-code",
      body: route.request().postDataJSON() as Record<string, unknown>
    });
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ accepted: true }) });
  });
  await page.route("**/api/v1/auth/password-reset/confirm", async (route) => {
    expectOriginOnlyAuthRequest(route);
    firstPasswordPayloads.push({
      path: "/auth/password-reset/confirm",
      body: route.request().postDataJSON() as Record<string, unknown>
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_steam_only",
          email: "linked@example.test",
          display_name: "SteamPlayer",
          status: "active",
          created_at: "2026-08-13T12:00:00Z",
          roles: ["authenticated_user", "player"],
          can_create_public_tournaments: false,
          steam_id: "76561198999999999",
          steam_linked: true,
          has_password: true
        },
        expires_at: "2026-08-14T12:00:00Z"
      })
    });
  });
  const documentNavigations = await page.evaluate(() => performance.getEntriesByType("navigation").length);
  await setPasswordLink.click();
  await page.getByLabel("Email").fill("linked@example.test");
  await page.getByRole("button", { name: "Отправить код" }).click();
  await page.getByLabel("Код подтверждения").fill("123456");
  await page.getByRole("button", { name: "ОК" }).click();
  await page.getByLabel("Новый пароль", { exact: true }).fill("First-password-1");
  await page.getByLabel("Повторите новый пароль").fill("First-password-1");
  await page.getByRole("button", { name: "Сменить пароль" }).click();
  await expect(page).toHaveURL(/\/profile\/me\?tab=account$/u);
  await expect(page.locator("form.account-security-card")).toBeVisible();
  await expect(page.locator(".account-passwordless-card")).toHaveCount(0);
  expect(await page.evaluate(() => performance.getEntriesByType("navigation").length)).toBe(documentNavigations);
  expect(firstPasswordPayloads).toEqual([
    { path: "/auth/password-reset/request", body: { email: "linked@example.test" } },
    { path: "/auth/password-reset/verify-code", body: { email: "linked@example.test", code: "123456" } },
    {
      path: "/auth/password-reset/confirm",
      body: { email: "linked@example.test", code: "123456", new_password: "First-password-1" }
    }
  ]);
});

test("Steam sign-up needs no email and existing accounts link through the verified Steam redirect", async ({ page }) => {
  const startPayloads: Array<{ path: string; body: Record<string, unknown> }> = [];
  const csrfTokenRequests = await trackCsrfTokenRequests(page);
  let submittedSteamReturnTo = "";
  await page.route("**/api/v1/auth/steam/login/start", async (route) => {
    expectOriginOnlyAuthRequest(route);
    const body = route.request().postDataJSON() as Record<string, unknown>;
    startPayloads.push({
      path: new URL(route.request().url()).pathname,
      body
    });
    submittedSteamReturnTo = String(body.return_to ?? "");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authorization_url: "/auth/login?steam_auth=error",
        expires_at: "2026-08-13T12:05:00Z"
      })
    });
  });

  await page.goto("/auth/register");
  await page.getByRole("button", { name: "Создать аккаунт через Steam" }).click();
  await expect(page).toHaveURL(/\/auth\/login\?steam_auth=error$/u);
  expect(startPayloads).toHaveLength(1);
  expect(startPayloads[0].path).toBe("/api/v1/auth/steam/login/start");
  expect(submittedSteamReturnTo).toContain("/auth/steam-complete?returnTo=%2Fprofile%2Fme");
  expect(csrfTokenRequests).toEqual([]);

  await authenticateTestUser(page, [{ name: "password-only-smoke", value: "1" }]);
  let linkPayload: Record<string, unknown> | null = null;
  await page.route("**/api/v1/auth/steam/link/start", async (route) => {
    linkPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authorization_url: "/auth/steam-complete?returnTo=%2Fprofile%2Fme%3Ftab%3Daccount&flow=link&steam_auth=error",
        expires_at: "2026-08-13T12:05:00Z"
      })
    });
  });
  await page.goto("/profile/me?tab=account");
  await page.getByRole("button", { name: "Привязать Steam" }).click();
  await expect(page).toHaveURL(/\/profile\/me\?tab=account&steam_auth=error$/u);
  expect(linkPayload).toEqual({
    return_to: "/auth/steam-complete?returnTo=%2Fprofile%2Fme%3Ftab%3Daccount&flow=link"
  });
  await expect(page.getByText("Не удалось привязать Steam", { exact: false })).toBeVisible();
});

test("Steam completion route preserves a safe destination and localizes provider errors", async ({ page }) => {
  await page.goto("/auth/steam-complete?returnTo=%2Ftournaments&steam_auth=success");
  await expect(page).toHaveURL(/\/tournaments$/u);

  await page.goto("/auth/steam-complete?returnTo=https%3A%2F%2Fevil.example&steam_auth=error");
  await expect(page).toHaveURL(/\/auth\/login\?steam_auth=error&returnTo=%2F$/u);
  await expect(page.getByText("Не удалось завершить вход через Steam. Попробуйте ещё раз")).toBeVisible();

  await authenticateTestUser(page, [{ name: "password-only-smoke", value: "1" }]);
  await page.goto("/auth/steam-complete?returnTo=%2Fprofile%2Fme%3Ftab%3Daccount&flow=link&steam_auth=error");
  await expect(page).toHaveURL(/\/profile\/me\?tab=account&steam_auth=error$/u);
  await expect(page.getByText("Не удалось привязать Steam", { exact: false })).toBeVisible();

  await page.context().clearCookies();
  await authenticateTestUser(page, [{ name: "steam-only-smoke", value: "1" }]);
  await page.goto("/auth/steam-complete?returnTo=%2Fprofile%2Fme%3Ftab%3Daccount&flow=link&steam_auth=success");
  await expect(page).toHaveURL(/\/profile\/me\?tab=account&steam_auth=success$/u);
  await expect(page.getByRole("status")).toContainText("Steam успешно привязан");
});

test("profile distinguishes authentication from transient account API failures", async ({ page }) => {
  await authenticateTestUser(page, [{ name: "profile-unavailable-smoke", value: "1" }]);
  await page.goto("/profile/me?tab=account");
  await expect(page.getByRole("link", { name: "Профиль: lisalexy" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Профиль временно недоступен" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Нужен вход" })).toHaveCount(0);

  await page.context().clearCookies();
  await authenticateTestUser(page, [{ name: "users-me-unavailable-smoke", value: "1" }]);
  await page.goto("/profile/me?tab=account");
  await expect(page.getByRole("button", { name: "Повторить проверку сессии" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Профиль временно недоступен" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Привязать Steam" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Привязать по коду" })).toHaveCount(0);

  const navigationEntriesBefore = await page.evaluate(() => performance.getEntriesByType("navigation").length);
  await page.context().clearCookies();
  await authenticateTestUser(page);
  await page.getByRole("button", { name: "Повторить проверку сессии" }).click();
  await expect(page.getByRole("link", { name: "Профиль: lisalexy" })).toBeVisible();
  await page.getByRole("button", { name: "Повторить", exact: true }).click();
  await expect(page.getByText("Фотография профиля", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => performance.getEntriesByType("navigation").length)).toBe(navigationEntriesBefore);
});

test("authoritative session expiry immediately removes the mounted profile editor", async ({ page }) => {
  await authenticateTestUser(page);
  let rejectedMutations = 0;
  await page.route("**/api/v1/profiles/me", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.fallback();
      return;
    }
    rejectedMutations += 1;
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Session is invalid." })
    });
  });

  await page.goto("/profile/me?tab=account");
  await page.getByLabel("Ник").fill("ExpiredPlayer");
  await page.getByTestId("profile-save-account-button").click();
  await expect(page.getByRole("heading", { name: "Нужен вход" })).toBeVisible();
  await expect(page.getByText("Фотография профиля", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("main").getByRole("link", { name: "Войти", exact: true })).toBeVisible();
  expect(rejectedMutations).toBe(1);
});

test("auth buttons remain disabled while one delayed mutation is in flight", async ({ page }) => {
  let loginRequests = 0;
  const csrfTokenRequests = await trackCsrfTokenRequests(page);
  await page.route("**/api/v1/auth/login", async (route) => {
    expectOriginOnlyAuthRequest(route);
    loginRequests += 1;
    await new Promise((resolve) => setTimeout(resolve, 700));
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Invalid credentials." })
    });
  });
  await page.goto("/auth/login");
  await page.getByLabel("Email").fill("delayed@example.test");
  await page.getByLabel("Пароль").fill("long-password");
  const loginButton = page.getByRole("button", { name: "Войти", exact: true });
  await loginButton.dblclick();
  await expect(page.getByRole("button", { name: "Вход..." })).toBeDisabled();
  await expect(page.getByText("Не удалось войти.")).toBeVisible();
  expect(loginRequests).toBe(1);
  expect(csrfTokenRequests).toEqual([]);
});

test("registration and check-in actions work before teams are formed", async ({ page }) => {
  let registered = false;
  let checkedIn = false;
  let leaveCount = 0;

  await page.clock.install({ time: new Date("2026-06-07T15:35:00Z") });
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "registration-smoke-session",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "registration-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "teams-pending-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    }
  ]);

  await page.route("**/api/v1/tournaments/night-veil-open-5/join", async (route) => {
    if (route.request().method() === "DELETE") {
      registered = false;
      leaveCount += 1;
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "You are not registered in this tournament." })
      });
      return;
    }

    registered = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "reg_1",
        user_id: "u_lisalexy",
        status: "registered",
        check_in_status: "pending",
        registered_at: "2026-05-14T12:00:00Z",
        checked_in_at: null
      })
    });
  });

  await page.route("**/api/v1/tournaments/night-veil-open-5/deadlock/ready-check/vote", async (route) => {
    const choice = (route.request().postDataJSON() as { choice: "yes" | "no" }).choice;
    checkedIn = choice === "yes";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        round_id: 1,
        tournament_id: "t_1",
        status: "active",
        eligible_participant_count: 32,
        current_user_choice: choice,
        changed: true,
        server_received_at: "2026-06-07T15:31:00Z"
      })
    });
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect(page.getByRole("button", { name: "Зарегистрироваться" })).toBeEnabled();
  await expect(page.getByText("Не выполнено").first()).toBeVisible();
  await expect(page.getByRole("main").getByTestId("registration-steps").getByText("Формирование команд", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByTestId("registration-steps").getByRole("button", { name: "Формирование команд" })).toHaveCount(0);
  await expect(page.locator(".team-auto-text")).toContainText("~");
  await expect(page.locator(".steps-panel .step-number")).toHaveCount(0);
  await expect(page.locator(".steps-panel .step-title")).toHaveCount(0);
  const firstStepButtonBox = await page.getByRole("button", { name: "Зарегистрироваться" }).boundingBox();
  const firstStepNoteBox = await page.locator(".steps-panel .step-note").first().boundingBox();
  const firstStepGap = (firstStepNoteBox?.y ?? 0) - ((firstStepButtonBox?.y ?? 0) + (firstStepButtonBox?.height ?? 0));
  expect(firstStepGap).toBeGreaterThanOrEqual(0);
  expect(firstStepGap).toBeLessThanOrEqual(6);
  if ((page.viewportSize()?.width ?? 0) > 1250) {
    const arrowBox = await page.locator(".steps-panel .arrow").first().boundingBox();
    const buttonBox = await page.getByRole("button", { name: "Зарегистрироваться" }).boundingBox();
    expect(arrowBox).not.toBeNull();
    expect(buttonBox).not.toBeNull();
    const arrowCenterY = arrowBox!.y + arrowBox!.height / 2;
    const buttonCenterY = buttonBox!.y + buttonBox!.height / 2;
    expect(Math.abs(arrowCenterY - buttonCenterY)).toBeLessThanOrEqual(4);
  }
  await page.getByRole("button", { name: "Зарегистрироваться" }).click();
  await expect(page.getByRole("button", { name: "Отменить регистрацию" })).toBeEnabled();
  expect(leaveCount).toBe(0);
  await page.getByRole("button", { name: "Подтвердить участие" }).click();
  const cancelConfirmationButton = page.getByRole("button", { name: "Отменить подтверждение" });
  await expect(cancelConfirmationButton).toBeVisible();
  await expect(cancelConfirmationButton).toBeEnabled();
  await expect(cancelConfirmationButton.locator("..")).toHaveClass(/done/);
  await cancelConfirmationButton.click();
  await expect(page.getByRole("button", { name: "Подтвердить участие" })).toBeEnabled();
  expect(checkedIn).toBe(false);

  await page.getByRole("button", { name: "Подтвердить участие" }).click();
  await expect(page.getByRole("button", { name: "Отменить подтверждение" })).toBeEnabled();
  await page.clock.fastForward(25 * 60 * 1000 + 100);
  await expect(page.getByRole("main").getByTestId("registration-steps").getByText("Участие подтверждено", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByTestId("registration-steps").getByRole("button", { name: "Участие подтверждено" })).toHaveCount(0);
  await expect(page.getByRole("main").getByTestId("registration-steps").getByText("Зарегистрирован", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByTestId("registration-steps").getByRole("button", { name: "Зарегистрирован" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Отменить регистрацию" })).toHaveCount(0);
  expect(registered).toBe(true);
  expect(checkedIn).toBe(true);
});

test("formed teams replace registration control with a status", async ({ page }) => {
  await page.context().addCookies([
    { name: "deadlock_platform_session", value: "formed-teams-session", url: "http://127.0.0.1:3100" },
    { name: "registration-smoke", value: "1", url: "http://127.0.0.1:3100" }
  ]);

  await page.goto("/tournaments/night-veil-open-5");
  const steps = page.getByRole("main").getByTestId("registration-steps");
  await expect(steps.getByText("Регистрация закрыта", { exact: true })).toBeVisible();
  await expect(steps.getByRole("button", { name: "Зарегистрироваться" })).toHaveCount(0);
  await expect(steps.getByRole("button", { name: "Регистрация закрыта" })).toHaveCount(0);
});

test("tournament detail aligns its sections and keeps match formats with the bracket", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1920, height: 1080 });
  }
  await page.goto("/tournaments/night-veil-open-5");

  const description = page.locator(".description-panel");
  const bracket = page.getByRole("main").locator(".bracket-panel");
  await expect(description.getByText("Формат матчей", { exact: true })).toHaveCount(0);
  await expect(description.getByText("Формат финала", { exact: true })).toHaveCount(0);
  await expect(bracket).toContainText("Матчи");
  await expect(bracket).toContainText("Финал");
  await expect(bracket.locator(".bracket-format-summary")).toHaveText(["МатчиBO3", "ФиналBO3"]);
  await expect(bracket.getByText("Сетка откроется после формирования команд и посева первого раунда.")).toHaveCount(0);
  await expect(bracket.getByRole("link", { name: "Перейти к сетке" })).toBeVisible();
  await expect(bracket.locator(".bracket-format-summary").first()).toHaveCSS("border-left-width", "0px");
  const bracketItemGeometry = await bracket.locator(".bracket-panel-title, .bracket-format-group, .outline-button").evaluateAll(
    (items) => items.map((item) => {
      const box = item.getBoundingClientRect();
      return { left: box.left, right: box.right };
    })
  );
  if ((page.viewportSize()?.width ?? 0) > 820) {
    const availableCenter = (bracketItemGeometry[0].right + bracketItemGeometry[2].left) / 2;
    const formatsCenter = (bracketItemGeometry[1].left + bracketItemGeometry[1].right) / 2;
    expect(Math.abs(availableCenter - formatsCenter)).toBeLessThanOrEqual(1);
    const bracketBox = await bracket.boundingBox();
    expect(bracketBox).not.toBeNull();
    expect(Math.abs(bracketBox!.x + bracketBox!.width - 22 - bracketItemGeometry.at(-1)!.right)).toBeLessThanOrEqual(1);
  }
  const detailButtonStyles = await bracket.getByRole("link", { name: "Перейти к сетке" }).evaluate((element) => ({
    backdrop: getComputedStyle(element).backdropFilter,
    highlight: getComputedStyle(element, "::before").backgroundImage,
  }));
  expect(detailButtonStyles.backdrop).toBe("none");
  expect(detailButtonStyles.highlight).toBe("none");

  const geometry = await page.locator("main.main").evaluate((main) => {
    const card = main.querySelector(".top-detail-grid > .tournament-card")!.getBoundingClientRect();
    const descriptionBox = main.querySelector(".description-panel")!.getBoundingClientRect();
    const top = main.querySelector(".top-detail-grid")!.getBoundingClientRect();
    const steps = main.querySelector(".steps-panel")!.getBoundingClientRect();
    const info = main.querySelector(".info-grid")!.getBoundingClientRect();
    return {
      horizontalGap: descriptionBox.left - card.right,
      topToSteps: steps.top - top.bottom,
      stepsToInfo: info.top - steps.bottom
    };
  });
  expect(geometry.horizontalGap).toBeLessThanOrEqual(16);
  expect(Math.abs(geometry.topToSteps - geometry.stepsToInfo)).toBeLessThanOrEqual(1);

  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("tournament-detail-1920-loaded.png")
    });
  }
});

test("participation confirmation closes immediately at the deadline when not confirmed", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:59:50Z") });
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "registration-deadline-session",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "registration-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "teams-pending-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    }
  ]);  await page.route("**/api/v1/tournaments/night-veil-open-5/join", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "reg_deadline",
        user_id: "u_lisalexy",
        status: "registered",
        check_in_status: "pending",
        registered_at: "2026-06-07T15:59:50Z",
        checked_in_at: null
      })
    });
  });

  await page.goto("/tournaments/night-veil-open-5");
  await page.getByRole("button", { name: "Зарегистрироваться" }).click();
  await expect(page.getByRole("button", { name: "Подтвердить участие" })).toBeEnabled();
  await page.clock.fastForward(10_100);
  await expect(page.getByRole("button", { name: "Подтверждение закрыто" })).toBeDisabled();
});

test("registration button is unavailable outside registration_open state", async ({ page }) => {
  await authenticateTestUser(page, [{ name: "registration-smoke", value: "1" }]);
  await page.goto("/tournaments/citadel-clash-3");

  await expect(page.getByRole("button", { name: "Зарегистрироваться" })).toHaveCount(0);
  await expect(page.getByRole("main").getByTestId("registration-steps").locator(".status-action").first()).toHaveText("Регистрация закрыта");
  await expect(page.getByRole("main").getByTestId("registration-steps").getByText("Команды сформированы", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByTestId("registration-steps").getByRole("button", { name: "Команды сформированы" })).toHaveCount(0);
});

test("team panels stay hidden for visitors who are not registered", async ({ page }) => {
  await page.goto("/tournaments/night-veil-open-5");

  await expect(page.getByText("Моя команда", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Команды соперников", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("main").getByTestId("tournament-team-unassigned")).toHaveCount(0);
});

test("registered player outside the published roster sees the unassigned state", async ({ page }) => {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "team-unassigned-smoke",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "team-unassigned-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    }
  ]);
  await page.goto("/tournaments/night-veil-open-5");
  await expect(page.getByRole("main").getByTestId("tournament-team-unassigned")).toContainText(
    "К сожалению, вы не попали ни в одну команду"
  );
  await expect(page.getByText("Моя команда", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Команды соперников", { exact: true })).toHaveCount(0);
});

test("active team commitment warns before formation and becomes generic afterwards", async ({ page }) => {
  await authenticateTestUser(page, [
    { name: "commitment-blocked-smoke", value: "1" },
    { name: "teams-pending-smoke", value: "1" }
  ]);

  await page.goto("/tournaments/citadel-clash-3");
  await expect(page.getByTestId("tournament-commitment-blocked")).toContainText(
    "Вы можете зарегистрироваться и подтвердить готовность, но вы не попадете в команду на этом турнире, пока ваша команда «Синие» не завершит участие в турнире «Active Cup»."
  );
  await expect(page.getByRole("main").getByTestId("tournament-team-unassigned")).toHaveCount(0);

  await page.context().clearCookies({ name: "teams-pending-smoke" });
  await page.goto("/tournaments/night-veil-open-5");
  await expect(page.getByTestId("tournament-commitment-blocked")).toHaveCount(0);
  await expect(page.getByRole("main").getByTestId("tournament-team-unassigned")).toContainText(
    "К сожалению, вы не попали ни в одну команду"
  );
  await expect(page.getByText("Регистрация сохранена, но в опубликованный состав команд вы не вошли.")).toHaveCount(0);
});

test("tournament detail switches opponent roster panel and returns to team list", async ({ page }, testInfo) => {
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "team-member-smoke",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/tournaments/night-veil-open-5");
  const myTeamPanel = page.getByText("Моя команда", { exact: true }).locator("../..");
  const myIdentity = myTeamPanel.locator(".team-player-identity").filter({ hasText: "lisalexy" });
  await expect(myIdentity).toBeVisible();
  await expect(myIdentity.locator("img")).toHaveAttribute("src", /Abrams\.png/);
  const myRank = myTeamPanel.locator(".team-player-rank").first();
  await expect(myRank.locator("img")).toHaveAttribute("src", /Eternus\.webp/);
  await expect(myRank).toContainText("VI");
  const firstTeamRowBox = await myTeamPanel.getByRole("row").first().boundingBox();
  const myRankImageBox = await myRank.locator("img").boundingBox();
  const mySubrankBox = await myRank.locator("span").boundingBox();
  expect(Math.abs(
    (firstTeamRowBox!.x + firstTeamRowBox!.width / 2)
    - (myRankImageBox!.x + myRankImageBox!.width / 2)
  )).toBeLessThanOrEqual(1);
  expect(mySubrankBox!.x).toBeGreaterThan(myRankImageBox!.x + myRankImageBox!.width);
  await expect(myTeamPanel.getByText("Carry", { exact: true })).toHaveCount(0);
  await expect(myTeamPanel.locator('.team-player-role-icon[aria-label="Капитан"]')).toHaveCount(1);
  await expect(myTeamPanel.locator('.team-player-role-icon[aria-label="Капитан"]')).toBeVisible();
  await expect(myTeamPanel.locator('.team-player-role-icon[aria-label="Замена"]')).toHaveCount(1);
  await expect(myTeamPanel.locator('.team-player-role-icon[aria-label="Замена"]')).toBeVisible();
  await expect(myTeamPanel.getByText("Капитан", { exact: true })).toHaveCount(0);
  await expect(myTeamPanel.getByText("Игрок", { exact: true })).toHaveCount(0);
  await expect(myTeamPanel.getByText("Замена", { exact: true })).toHaveCount(0);
  await expect(myTeamPanel.getByText(/\/7 игроков$/)).toHaveCount(0);
  await expect(myIdentity.locator("a")).toHaveCount(0);
  await expect(myTeamPanel.getByRole("link", { name: "Профиль" }).first()).toHaveAttribute("href", "/profile/me");
  const identityAlignment = await myIdentity.evaluate((node) => getComputedStyle(node).alignItems);
  expect(identityAlignment).toBe("center");
  await expect(page.getByText("Кол-во: 1", { exact: true })).toBeVisible();
  if ((page.viewportSize()?.width ?? 0) <= 520) {
    await expect(page.getByText("Соперники", { exact: true })).toBeVisible();
    await expect(page.getByText("Команды соперников", { exact: true })).toBeHidden();
  }
  const opponentPanel = page.locator(".opponent-team-panel");
  const profileControl = myTeamPanel.getByRole("link", { name: "Профиль" }).first();
  const countControl = opponentPanel.getByText("Кол-во: 1", { exact: true });
  const rosterControl = opponentPanel.getByRole("button", { name: "Состав" }).first();
  for (const control of [profileControl, countControl, rosterControl]) {
    const box = await control.boundingBox();
    expect(box?.width).toBeCloseTo(104, 0);
    expect(box?.height).toBeCloseTo(32, 0);
    const alignment = await control.evaluate((node) => {
      const style = getComputedStyle(node);
      return { alignItems: style.alignItems, justifyContent: style.justifyContent };
    });
    expect(alignment).toEqual({ alignItems: "center", justifyContent: "center" });
  }
  const countBox = await countControl.boundingBox();
  const rosterBox = await rosterControl.boundingBox();
  expect(Math.abs((countBox?.x ?? 0) - (rosterBox?.x ?? 0))).toBeLessThanOrEqual(1);
  const teamMemberRows = myTeamPanel.getByRole("row");
  const firstRowBox = await teamMemberRows.nth(0).boundingBox();
  const secondRowBox = await teamMemberRows.nth(1).boundingBox();
  expect(firstRowBox?.height).toBeCloseTo(56, 0);
  expect(secondRowBox?.height).toBeCloseTo(56, 0);
  const modernSeparator = await teamMemberRows.nth(1).locator("td").first().evaluate((node) => getComputedStyle(node).borderTopColor);
  expect(modernSeparator).toBe("rgba(167, 139, 250, 0.13)");
  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("tournament-team-ranks-1920.png")
    });
  }
  const opponentScroll = page.locator(".opponents-scroll");
  const opponentScrollHeight = await opponentScroll.evaluate((node) => node.clientHeight);
  expect(opponentScrollHeight).toBeGreaterThan(300);
  await page.getByRole("button", { name: "Состав" }).first().click();
  const shadowHawkRow = page.getByRole("row").filter({ hasText: "ShadowHawk" });
  await expect(shadowHawkRow).toBeVisible();
  const shadowProfileLink = shadowHawkRow.getByRole("link", { name: "Профиль" });
  await expect(shadowProfileLink).toHaveAttribute("href", "/tournaments/night-veil-open-5/profiles/u_shadow");
  const opponentProfileControlBox = await shadowProfileLink.boundingBox();
  expect(opponentProfileControlBox?.width).toBeCloseTo(104, 0);
  expect(opponentProfileControlBox?.height).toBeCloseTo(32, 0);
  expect(Math.abs((opponentProfileControlBox?.x ?? 0) - (rosterBox?.x ?? 0))).toBeLessThanOrEqual(1);
  await shadowProfileLink.click();
  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5\/profiles\/u_shadow$/);
  await expect(page.getByRole("heading", { name: "ShadowHawk" })).toBeVisible();
  await expect(page.getByText("shadowhawk@example.test")).toBeVisible();
  await expect(page.locator(".public-profile-rank img")).toHaveAttribute("src", /Oracle\.webp/);
  await expect(page.locator(".public-profile-rank")).toContainText("Oracle V");
  await expect(page.locator(".public-profile-view")).toBeVisible();
  await expect(page.getByText("Профиль заполнен", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Турнирный профиль" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Контакты" })).toHaveCount(0);
  const publicProfileStats = page.locator(".public-profile-stat");
  await expect(publicProfileStats).toHaveCount(1);
  await expect(publicProfileStats).toHaveCSS("border-top-width", "0px");
  await expect(publicProfileStats).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  const rankBox = await page.locator(".public-profile-rank").boundingBox();
  const hoursBox = await publicProfileStats.boundingBox();
  if ((page.viewportSize()?.width ?? 0) > 820) {
    expect(hoursBox!.x).toBeGreaterThan(rankBox!.x + rankBox!.width);
  } else {
    expect(hoursBox!.y).toBeGreaterThan(rankBox!.y + rankBox!.height);
  }
  const scopedContactRows = page.locator(".public-profile-contact");
  await expect(scopedContactRows).toHaveCount(4);
  await expect(scopedContactRows.first()).toHaveCSS("border-top-width", "0px");
  await expect(scopedContactRows.nth(1)).toHaveCSS("border-top-width", "1px");
  await expect(scopedContactRows.first()).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(page.locator(".public-profile-contact-icon svg")).toHaveCount(4);
  await expect(page.locator('.public-profile-contact-icon svg[data-brand="discord"]')).toHaveCount(1);
  await expect(page.locator('.public-profile-contact-icon svg[data-brand="steam"]')).toHaveCount(1);
  await expect(page.locator(".public-profile-hero img").first()).toHaveCSS("object-position", "50% 50%");
  const publicProfileHeroes = page.locator(".public-profile-hero");
  await expect(publicProfileHeroes).toHaveCount(3);
  if ((page.viewportSize()?.width ?? 0) <= 520) {
    const heroBoxes = await Promise.all([0, 1, 2].map((index) => publicProfileHeroes.nth(index).boundingBox()));
    expect(heroBoxes.every((box) => Math.abs((box?.y ?? 0) - (heroBoxes[0]?.y ?? 0)) <= 1)).toBe(true);
  }
  const profileBackLink = page.getByRole("link", { name: "Назад к турниру" });
  await expect(profileBackLink.locator("svg")).toBeVisible();
  expect(await profileBackLink.evaluate((node) => getComputedStyle(node).textDecorationLine)).toBe("none");
  const profileBackBox = await profileBackLink.boundingBox();
  const profileMain = page.locator("main");
  const profileMainBox = await profileMain.boundingBox();
  const profileMainPaddingRight = Number.parseFloat(await profileMain.evaluate((element) => getComputedStyle(element).paddingRight));
  expect(Math.abs(
    (profileBackBox?.x ?? 0) + (profileBackBox?.width ?? 0)
    - ((profileMainBox?.x ?? 0) + (profileMainBox?.width ?? 0) - profileMainPaddingRight)
  )).toBeLessThanOrEqual(1);
  await profileBackLink.click();
  await expect(page).toHaveURL(/\/tournaments\/night-veil-open-5$/);
  const restoredRosterControl = page.getByRole("button", { name: "Состав" }).first();
  await expect(restoredRosterControl).toBeVisible();
  const backControl = page.getByRole("button", { name: "Назад" });
  await page.getByRole("button", { name: "Состав" }).first().click();
  await expect(backControl).toBeVisible();
  const backControlBox = await backControl.boundingBox();
  expect(backControlBox?.width).toBeCloseTo(104, 0);
  expect(backControlBox?.height).toBeCloseTo(32, 0);
  expect(Math.abs((backControlBox?.x ?? 0) - (rosterBox?.x ?? 0))).toBeLessThanOrEqual(1);
});

test("public profile exposes unboxed contact data with transient copy feedback", async ({ page }, testInfo) => {
  await page.goto("/profile/lisalexy");

  await expect(page.getByRole("heading", { name: "lisalexy" })).toBeVisible();
  await expect(page.getByText("player@example.com")).toBeVisible();
  await expect(page.getByText("lisalexy#4821")).toBeVisible();
  await expect(page.getByText("76561198000000000")).toBeVisible();
  await expect(page.getByText("Профиль заполнен", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Игровые параметры и выбранный пул героев", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Данные для связи с участником", { exact: true })).toHaveCount(0);
  await expect(page.locator(".public-profile-copy-button")).toHaveCount(4);
  const emailCopyButton = page.getByRole("button", { name: "Скопировать почту" });
  await emailCopyButton.click();
  const copiedEmailButton = page.getByRole("button", { name: "Почта: скопировано" });
  await expect(copiedEmailButton).toBeVisible();
  await expect(copiedEmailButton).toBeDisabled();
  await expect(copiedEmailButton.locator(".lucide-check")).toHaveCount(1);
  if (testInfo.project.name === "desktop") {
    await expect(emailCopyButton).toBeVisible({ timeout: 3500 });
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("public-profile-unboxed-1920.png")
    });
  }
});

test("profile editor saves tournament profile through API", async ({ page }, testInfo) => {
  let requestBody: unknown;
  let accountRequestBody: Record<string, unknown> | null = null;
  let securityRequestBody: Record<string, unknown> | null = null;
  let avatarRequestCount = 0;

  await page.route("**/api/v1/profiles/me/avatar", async (route) => {
    avatarRequestCount += 1;
    await route.fulfill({ status: 500, body: "oversized avatar must not reach API" });
  });

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.route("**/api/v1/profiles/me", async (route) => {
    if (route.request().method() === "PUT") {
      accountRequestBody = route.request().postDataJSON() as Record<string, unknown>;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        account_email: "player@example.com",
        display_name: accountRequestBody?.display_name ?? "lisalexy",
        handle: "lisalexy",
        avatar_url: null,
        contact_email: accountRequestBody?.contact_email ?? "player@example.com",
        discord_account: accountRequestBody?.discord_account ?? "lisalexy#4821",
        steam_id: "76561198000000000",
        region: accountRequestBody?.region ?? "Finland, Kannus",
        completion_percent: 82
      })
    });
  });

  await page.route("**/api/v1/auth/account", async (route) => {
    securityRequestBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        email: securityRequestBody.email ?? "changed@example.com",
        display_name: "lisalexy",
        status: "active",
        created_at: new Date().toISOString(),
        roles: [],
        can_create_public_tournaments: false
      })
    });
  });

  await page.route("**/api/v1/profiles/me/deadlock", async (route) => {
    if (route.request().method() === "PUT") {
      requestBody = route.request().postDataJSON();
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u_lisalexy",
        rank: route.request().method() === "PUT" ? "Phantom" : "Eternus",
        subrank: 6,
        playtime: "3000+",
        roles: ["Carry", "Semi-Carry", "Support"],
        pool: ["Abrams", "Apollo", "Bebop"],
        captain_priority: "neutral",
        updated_at: new Date().toISOString()
      })
    });
  });

  await page.route("**/api/v1/profiles/me/deadlock/dream-slots", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([])
    });
  });

  await page.goto("/profile/me");
  await expect(page.getByText("Единая шкала учитывает")).toHaveCount(0);
  await expect(page.getByTestId("profile-completion")).toHaveText("Профиль заполнен на 60%");
  await expect(page.locator(".profile-summary .profile-avatar")).toHaveCSS("border-radius", "50%");
  await expect(page.locator(".profile-summary .profile-avatar svg")).toHaveCount(1);
  await expect(page.locator(".tournament-profile-panel")).toHaveCSS("min-height", "540px");
  const wideProfileSummary = (page.viewportSize()?.width ?? 0) > 1250;
  await expect(page.locator(".completion")).toHaveCSS("justify-self", wideProfileSummary ? "end" : "center");
  await expect(page.locator(".completion > .panel-title")).toHaveCSS("justify-content", "flex-start");
  await expect(page.locator(".profile-summary").getByRole("button", { name: /Выйти/ })).toHaveCount(0);
  await expect(page.locator(".progress-row").locator(":scope > *")).toHaveCount(1);
  const tournamentProfileActions = page.locator(".tournament-profile-actions");
  await expect(tournamentProfileActions).toHaveCount(1);
  await expect(tournamentProfileActions).toHaveCSS("justify-content", "flex-end");
  await expect(tournamentProfileActions).toHaveCSS("border-top-width", "0px");
  await expect(tournamentProfileActions.locator("button").nth(0)).toContainText("Отменить");
  await expect(tournamentProfileActions.locator("button").nth(1)).toContainText("Сохранить");
  await expect(page.getByTestId("profile-save-heroes-button")).toHaveCount(0);
  await expect(page.getByTestId("profile-cancel-heroes-button")).toHaveCount(0);
  const tournamentProfileColumns = page.locator(".tournament-profile-columns");
  if ((page.viewportSize()?.width ?? 0) > 1100) {
    await expect(tournamentProfileColumns).toHaveCSS("grid-template-columns", /.+ .+/);
  }
  await expect(page.locator(".tournament-profile-heroes")).toHaveCSS("border-left-width", "0px");
  await expect(page.locator(".tournament-profile-heroes")).toHaveCSS("border-top-width", "0px");
  await expect(page.locator(".captain-banner .lucide-sword")).toHaveCount(1);
  await expect(page.locator(".captain-banner .lucide-shield")).toHaveCount(0);
  const rankCards = page.locator(".profile-rank-card");
  await expect(rankCards).toHaveCount(11);
  await expect(rankCards.locator("img")).toHaveCount(11);
  expect(await rankCards.locator("img").evaluateAll((images) => images.every((image) => image.getAttribute("src")?.includes(".webp")))).toBe(true);
  await expect.poll(() => rankCards.locator("img").evaluateAll((images) => images.every((image) => (
    (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth >= 256
  )))).toBe(true);
  await expect(page.locator(".profile-rank-picker > .label")).toHaveCount(0);
  expect(await page.getByTestId("profile-pill-eternus").evaluate((element) => getComputedStyle(element, "::after").content)).toBe('"✓"');
  if ((page.viewportSize()?.width ?? 0) > 820) {
    const rankBoxes = await Promise.all([0, 1, 2, 3, 4, 5, 6].map((index) => rankCards.nth(index).boundingBox()));
    expect(rankBoxes.slice(0, 6).every((box) => Math.abs((box?.y ?? 0) - (rankBoxes[0]?.y ?? 0)) <= 1)).toBe(true);
    expect(rankBoxes[6]!.y).toBeGreaterThan(rankBoxes[0]!.y);
    const heroCardBox = await page.locator(".tournament-profile-heroes .hero-card").first().boundingBox();
    expect(rankBoxes[0]!.width).toBeCloseTo(heroCardBox!.width, 0);
    expect(rankBoxes[0]!.height).toBeCloseTo(heroCardBox!.height, 0);
    expect(Math.abs(rankBoxes[0]!.y - heroCardBox!.y)).toBeLessThanOrEqual(1);
    const rankGridBox = await page.locator(".profile-rank-grid").boundingBox();
    const subrankBlock = page.locator(".profile-settings-list > div").nth(1);
    const subrankLabelBox = await subrankBlock.locator(".label").boundingBox();
    const subrankControlsBox = await subrankBlock.locator(".pill-row").boundingBox();
    const betweenGroups = subrankLabelBox!.y - (rankGridBox!.y + rankGridBox!.height);
    const withinGroup = subrankControlsBox!.y - (subrankLabelBox!.y + subrankLabelBox!.height);
    expect(betweenGroups).toBeGreaterThan(withinGroup);
  }
  await expect(page.getByTestId("profile-save-settings-button")).toBeDisabled();
  await expect(page.getByTestId("profile-cancel-settings-button")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Billy" })).toBeDisabled();
  const sourceHero = page.getByRole("button", { name: "Source New Hero" });
  await expect(sourceHero).toBeVisible();
  await expect(sourceHero).toBeDisabled();
  await page.getByRole("button", { name: "Abrams" }).click();
  await expect(sourceHero).toBeEnabled();
  await page.getByRole("button", { name: "Abrams" }).click();
  await page.getByTestId("profile-pill-phantom").click();
  await expect(page.getByTestId("profile-save-settings-button")).toBeEnabled();
  await expect(page.getByTestId("profile-cancel-settings-button")).toBeEnabled();
  await page.getByTestId("profile-cancel-settings-button").click();
  await expect(page.getByTestId("profile-pill-eternus")).toHaveClass(/active/);
  await expect(page.getByTestId("profile-save-settings-button")).toBeDisabled();

  await page.getByTestId("profile-pill-phantom").click();
  await page.getByTestId("profile-save-settings-button").click();

  await expect(page.getByTestId("profile-save-settings-button")).toHaveText(/Сохранено/);
  expect(requestBody).toMatchObject({
    rank: "Phantom",
    subrank: 6,
    playtime: "3000+",
    roles: ["Carry", "Semi-Carry", "Support"],
    pool: ["Abrams", "Apollo", "Bebop"]
  });

  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("profile-tournament-1920-loaded.png")
    });
  }

  await page.getByRole("button", { name: "Аккаунт" }).click();
  await expect(page.locator('.account-input-wrap svg[data-brand="discord"]')).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Профиль и контакты" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Вход и безопасность" })).toHaveCount(0);
  await expect(page.locator(".account-settings-side")).toBeVisible();
  await expect(page.locator(".account-signoff-card")).toContainText("Спасибо, что вы с нами!");
  await expect(page.locator(".account-support-message > span")).toHaveText([
    "Есть идеи по улучшению сайта?",
    "Напишите нам через форму поддержки."
  ]);
  const supportLineCounts = await page.locator(".account-support-message > span").evaluateAll((lines) => lines.map((line) => {
    const style = getComputedStyle(line);
    return Math.round(line.getBoundingClientRect().height / Number.parseFloat(style.lineHeight));
  }));
  if ((page.viewportSize()?.width ?? 0) > 1100) {
    expect(supportLineCounts).toEqual([1, 1]);
  }
  await expect(page.locator(".account-signoff-card")).not.toContainText("support@old-sparky.com");
  await expect(page.getByRole("link", { name: "Открыть форму поддержки" })).toHaveAttribute("href", "/info#support");
  await expect(page.getByRole("link", { name: "Открыть форму поддержки" }).locator("svg")).toHaveCount(1);
  await expect(page.getByTestId("profile-logout-button")).toHaveText("Выйти из аккаунта");
  await expect(page.getByText("Исходный файл не больше 5 МБ.")).toBeVisible();
  await expect(page.locator(".account-avatar-preview")).toHaveCSS("border-radius", "50%");
  await expect(page.locator(".account-avatar-preview svg")).toHaveCount(1);
  const accountCardGaps = await page.locator(".account-settings-card:not(.account-signoff-card)").evaluateAll((cards) => cards.map((card) => {
    const cardBox = card.getBoundingClientRect();
    const firstChild = card.firstElementChild!.getBoundingClientRect();
    const lastChild = card.lastElementChild!.getBoundingClientRect();
    return {
      top: firstChild.top - cardBox.top,
      bottom: cardBox.bottom - lastChild.bottom
    };
  }));
  expect(
    accountCardGaps.every((gap) => gap.top <= 18 && gap.bottom <= 18),
    `Unexpected account card padding: ${JSON.stringify(accountCardGaps)}`
  ).toBe(true);
  const accountSecurityForm = page.locator("form.account-security-card");
  await expect(accountSecurityForm).toHaveCount(1);
  await expect(page.getByTestId("profile-save-security-button")).toHaveAttribute("type", "submit");
  await expect(page.locator(".account-security-fields")).toHaveCSS("grid-template-columns", /.+/);
  const passwordFieldTops = await page.locator(".account-security-fields > .account-field").evaluateAll(
    (fields) => fields.map((field) => Math.round(field.getBoundingClientRect().top))
  );
  expect(new Set(passwordFieldTops).size).toBe(3);
  const signoffHeight = await page.locator(".account-signoff-card").evaluate((card) => card.getBoundingClientRect().height);
  expect(signoffHeight).toBeLessThan(280);
  const signoffGeometry = await page.locator(".account-signoff-card").evaluate((card) => {
    const cardBox = card.getBoundingClientRect();
    const copy = card.querySelector<HTMLElement>(".account-signoff-copy")!.getBoundingClientRect();
    const logout = card.querySelector<HTMLElement>("[data-testid='profile-logout-button']")!.getBoundingClientRect();
    return {
      buttonBelowCopy: logout.top >= copy.bottom,
      copyInside: copy.left >= cardBox.left && copy.right <= cardBox.right,
      logoutInside: logout.left >= cardBox.left && logout.right <= cardBox.right
    };
  });
  if ((page.viewportSize()?.width ?? 0) > 1100) {
    expect(signoffGeometry.buttonBelowCopy).toBe(true);
    expect(signoffGeometry.copyInside).toBe(true);
    expect(signoffGeometry.logoutInside).toBe(true);
    const accountBottoms = await page.locator(".account-settings-grid > .account-settings-card, .account-signoff-card").evaluateAll(
      (cards) => cards.map((card) => Math.round(card.getBoundingClientRect().bottom))
    );
    expect(new Set(accountBottoms).size).toBe(1);
  }
  if (testInfo.project.name === "desktop") {
    await page.screenshot({
      fullPage: true,
      path: testInfo.outputPath("profile-account-loaded.png")
    });
  }

    const accountSaveButton = page.getByTestId("profile-save-account-button");
  const accountCancelButton = page.getByTestId("profile-cancel-account-button");
  const nicknameInput = page.getByLabel("Ник");
  const emailIdentityButton = page.locator(".account-email-identity .account-identity-button");

  await expect(nicknameInput).toHaveValue("lisalexy");
  await expect(nicknameInput).toHaveAttribute("maxlength", "15");
  await expect(emailIdentityButton).toContainText("player@example.com");
  await expect(page.getByLabel("Новая почта")).toHaveCount(0);
  await expect(page.getByLabel("Текущий пароль")).toHaveCount(1);
  await expect(accountSaveButton).toBeDisabled();
  await expect(accountCancelButton).toBeDisabled();

  await nicknameInput.fill("lisalexy2");
  await expect(accountSaveButton).toBeEnabled();
  await expect(accountCancelButton).toBeEnabled();

  await accountCancelButton.click();
  await expect(nicknameInput).toHaveValue("lisalexy");
  await expect(accountSaveButton).toBeDisabled();

  await nicknameInput.fill("lisalexy2");
  await accountSaveButton.click();
  await expect(accountSaveButton).toBeDisabled();

  await expect.poll(() => accountRequestBody).toEqual({
    display_name: "lisalexy2",
    contact_email: "player@example.com",
    discord_account: "lisalexy#4821",
    region: "Finland, Kannus"
  });
  expect(accountRequestBody).not.toHaveProperty("steam_id");

  const accountNewPassword = accountSecurityForm.getByLabel("Новый пароль", { exact: true });
  const accountConfirmPassword = accountSecurityForm.getByLabel("Повторите новый пароль");
  await expect(accountNewPassword).toHaveAttribute("name", "new_password");
  await expect(accountConfirmPassword).toHaveAttribute("name", "confirm_password");
  await expect(accountNewPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(accountConfirmPassword).toHaveAttribute("autocomplete", "new-password");
  await accountNewPassword.fill("New-password-2");
  await expect(accountConfirmPassword).toHaveValue("");
  await accountConfirmPassword.fill("New-password-2");
  await accountSecurityForm.getByLabel("Текущий пароль").fill("Current-password-1");
  await accountNewPassword.fill("New-password-3");
  await expect(accountConfirmPassword).toHaveValue("New-password-2");
  await accountConfirmPassword.press("Enter");
  await expect(page.getByTestId("profile-security-validation")).toContainText("Новый пароль и подтверждение не совпадают.");
  await expect(page.getByRole("dialog", { name: "Подтвердите изменение" })).toHaveCount(0);
  await accountNewPassword.fill("New-password-2");
  await accountConfirmPassword.press("Enter");
  await expect.poll(() => securityRequestBody).toMatchObject({
    current_password: "Current-password-1",
    email: null,
    new_password: "New-password-2"
  });

  await page.getByTestId("profile-avatar-input").setInputFiles({
    name: "too-large.png",
    mimeType: "image/png",
    buffer: Buffer.alloc(5 * 1024 * 1024 + 1)
  });
  await expect(page.getByText("Исходный файл должен быть не больше 5 МБ.")).toBeVisible();
  expect(avatarRequestCount).toBe(0);
});

test("account keeps avatar media controls while hiding profile background and active sessions", async ({ page }) => {
  const mediaCalls: Array<{ method: string; path: string }> = [];
  await authenticateTestUser(page);
  await page.route("**/api/v1/profiles/me/avatar", async (route) => {
    mediaCalls.push({ method: route.request().method(), path: new URL(route.request().url()).pathname });
    await route.fallback();
  });
  await page.goto("/profile/me");
  await page.getByRole("button", { name: "Аккаунт" }).click();
  await expect(page.getByRole("heading", { name: "Активные сессии" })).toHaveCount(0);
  await expect(page.getByTestId("profile-banner-input")).toHaveCount(0);
  await expect(page.getByText("Фон профиля", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Сменить", { exact: true })).toBeVisible();

  await page.getByTestId("profile-avatar-input").setInputFiles({
    name: "avatar.png",
    mimeType: "image/png",
    buffer: Buffer.from("prepared-avatar-smoke")
  });
  const preparedAvatar = page.locator(".account-avatar-preview img[data-media-source='prepared']");
  await expect(preparedAvatar).toBeVisible();
  await expect(preparedAvatar).toHaveAttribute("srcset", /128w.*256w.*512w/u);
  await expect(page.locator(".media-upload-status")).toHaveCount(0);

  const deleteAvatarButton = page.getByRole("button", { name: "Удалить фото" });
  await expect(deleteAvatarButton).toHaveCSS("border-top-width", "1px");
  await expect(deleteAvatarButton).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await deleteAvatarButton.click();
  await expect(page.locator(".account-avatar-preview img")).toHaveCount(0);
  await expect(page.locator(".media-upload-status")).toHaveCount(0);
  expect(mediaCalls).toEqual([
    { method: "POST", path: "/api/v1/profiles/me/avatar" },
    { method: "DELETE", path: "/api/v1/profiles/me/avatar" }
  ]);
  await expectNoHorizontalOverflow(page);
});

test("account email changes only after inline code confirmation", async ({ page }) => {
  let profilePutCount = 0;
  const emailChangePayloads: Array<Record<string, unknown>> = [];

  await authenticateTestUser(page);

  await page.route("**/api/v1/profiles/me", async (route) => {
    if (route.request().method() === "PUT") {
      profilePutCount += 1;
    }
    await route.fallback();
  });

  await page.route("**/api/v1/auth/email-change/request", async (route) => {
    emailChangePayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });

  await page.route("**/api/v1/auth/email-change/confirm", async (route) => {
    emailChangePayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        email: "pending-new@example.test",
        display_name: "lisalexy",
        status: "active",
        created_at: "2026-05-20T00:00:00Z",
        roles: ["authenticated_user", "player"],
        can_create_public_tournaments: false,
        has_password: true
      })
    });
  });

  await page.goto("/profile/me?tab=account");
  await page.getByRole("button", { name: "Турнирный профиль", exact: true }).click();
  await expect(page.locator("#profile-panel-tournament")).not.toHaveAttribute("hidden", "");
  await page.getByRole("button", { name: "Аккаунт", exact: true }).click();
  await expect(page.locator("#profile-panel-account")).not.toHaveAttribute("hidden", "");

  const emailInput = page.getByTestId("profile-account-email");
  const profileSaveButton = page.getByTestId("profile-save-account-button");
  await expect(emailInput).toHaveValue("player@example.com");
  await emailInput.fill("pending-new@example.test");
  await expect(profileSaveButton).toHaveAttribute("aria-disabled", "false");
  await profileSaveButton.click();
  await expect(page.getByRole("dialog", { name: "Подтвердите смену почты" })).toBeVisible();
  await page.getByRole("dialog", { name: "Подтвердите смену почты" }).getByLabel("Текущий пароль").fill("CurrentPassword123!");
  await page.getByRole("button", { name: "Продолжить", exact: true }).click();

  await expect(
    page.getByText("Код отправлен на pending-new@example.test")
  ).toBeVisible();

  await page.getByLabel("Код подтверждения").fill("654321");
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();

  await expect(
    page.getByText("pending-new@example.test", { exact: true })
  ).toBeVisible();

  expect(emailChangePayloads).toEqual([
    {
      email: "pending-new@example.test",
      current_password: "CurrentPassword123!"
    },
    { email: "pending-new@example.test", code: "654321" }
  ]);

  expect(profilePutCount).toBe(1);
});

test("profile editor validation blocks empty role payload before API", async ({ page }) => {
  let putCount = 0;

  await page.route("**/api/v1/profiles/me", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        handle: "lisalexy",
        completion_percent: 82
      })
    });
  });

  await page.route("**/api/v1/profiles/me/deadlock", async (route) => {
    if (route.request().method() === "PUT") {
      putCount += 1;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u_lisalexy",
        rank: "Eternus",
        subrank: 6,
        playtime: "3000+",
        roles: ["Carry", "Semi-Carry", "Support"],
        pool: ["Abrams", "Apollo", "Bebop"],
        captain_priority: "neutral",
        updated_at: new Date().toISOString()
      })
    });
  });

  await page.route("**/api/v1/profiles/me/deadlock/dream-slots", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([])
    });
  });

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.goto("/profile/me");
  await page.getByTestId("profile-pill-carry").click();
  await page.getByTestId("profile-pill-semi-carry").click();
  await page.getByTestId("profile-pill-support").click();
  await page.getByTestId("profile-save-settings-button").click();

  await expect(page.getByTestId("profile-settings-validation")).toContainText("Выберите минимум одну роль");
  expect(putCount).toBe(0);
});

test("captain profile uses one full-width dream-slot hero picker", async ({ page }) => {
  type CaptainRequest = {
    captain_team_name?: string;
    slots: Array<{ slot_number: number; allowed_roles: string[]; desired_heroes: string[] }>;
  };
  let requestBody: CaptainRequest | null = null;
  let captainProfileBody: Record<string, unknown> | null = null;
  let requestCount = 0;

  await page.route("**/api/v1/profiles/me", async (route) => {
    if (route.request().method() === "PUT") {
      captainProfileBody = route.request().postDataJSON() as Record<string, unknown>;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        handle: "lisalexy",
        captain_team_name: captainProfileBody?.captain_team_name ?? "OldTeam",
        completion_percent: 82
      })
    });
  });

  await page.route("**/api/v1/profiles/me/deadlock", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u_lisalexy",
        rank: "Eternus",
        subrank: 6,
        playtime: "3000+",
        roles: ["Carry", "Semi-Carry", "Support"],
        pool: ["Abrams", "Apollo", "Bebop"],
        captain_priority: "neutral",
        updated_at: new Date().toISOString()
      })
    });
  });

  await page.route("**/api/v1/profiles/me/captain", async (route) => {
    const payload = route.request().postDataJSON() as CaptainRequest;
    requestBody = payload;
    captainProfileBody = payload;
    requestCount += 1;
    const slots = payload.slots;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        captain_team_name: payload.captain_team_name ?? "OldTeam",
        dream_slots: Array.from({ length: 6 }, (_, index) => {
          const slot = slots.find((item) => item.slot_number === index + 1);
          return {
            user_id: "u_lisalexy",
            slot_number: index + 1,
            allowed_roles: slot?.allowed_roles ?? [],
            desired_heroes: slot?.desired_heroes ?? [],
            updated_at: new Date().toISOString()
          };
        })
      })
    });
  });

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.goto("/profile/me");
  await page.getByRole("button", { name: "Профиль капитана" }).click();
  await expect(page.getByRole("heading", { name: "Автоматическое формирование команд для капитана" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Профиль капитана", exact: true })).toHaveCount(0);
  await expect(page.getByText("Название команды и пожелания к тиммейтам сохраняются одним действием.")).toHaveCount(0);
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(0);
  await expect(page.locator(".dream-hero")).toHaveCount(0);

  const firstSlotCard = page.locator(".dream-slot-card").first();
  const firstSlotHeroes = firstSlotCard.locator(".dream-selected-heroes");
  const firstSlotToggle = page.getByTestId("profile-dream-slot-1-heroes-toggle");
  const firstSlotBadge = firstSlotCard.locator(".state-badge");
  const teamNameInput = page.getByTestId("profile-captain-team-name");
  const saveCaptainButton = page.getByTestId("profile-save-captain-button");
  const cancelCaptainButton = page.getByTestId("profile-cancel-captain-button");
  await expect(page.locator(".captain-settings-head")).toHaveCSS("display", "block");
  const teamNameLabel = page.locator(".captain-team-name-field .panel-subtitle");
  const teammateTitle = page.getByRole("heading", { name: "Пожелания по тиммейтам" });
  await expect(teamNameLabel).toHaveCSS("font-size", "17px");
  await expect(teamNameLabel).toHaveCSS("font-weight", "780");
  await expect(teammateTitle).toHaveCSS("font-size", "17px");
  await expect(teammateTitle).toHaveCSS("font-weight", "780");
  const teamNameLabelBox = await teamNameLabel.boundingBox();
  const teamNameInputBox = await teamNameInput.boundingBox();
  const teammateTitleBox = await teammateTitle.boundingBox();
  const teammateCardsGridBox = await page.locator(".teammates-panel .teammate-grid").boundingBox();
  const teamNameGap = (teamNameInputBox?.y ?? 0) - ((teamNameLabelBox?.y ?? 0) + (teamNameLabelBox?.height ?? 0));
  const teammateGap = (teammateCardsGridBox?.y ?? 0) - ((teammateTitleBox?.y ?? 0) + (teammateTitleBox?.height ?? 0));
  expect(Math.abs(teamNameGap - teammateGap)).toBeLessThanOrEqual(1);
  await expect(firstSlotCard).toHaveCSS("gap", "16px");
  await expect(firstSlotCard).toHaveCSS("min-height", "0px");
  await expect(firstSlotCard.locator(".dream-slot-section").first()).toHaveCSS("gap", "10px");
  await expect(firstSlotBadge).toHaveText("Не настроен");
  await expect(teamNameInput).toHaveValue("OldTeam");
  await expect(saveCaptainButton).toBeDisabled();
  await expect(cancelCaptainButton).toBeDisabled();

  await teamNameInput.fill("SmokeTeam");
  const carryButton = firstSlotCard.getByRole("button", { name: "Carry", exact: true });
  await carryButton.click();
  await expect(firstSlotBadge).toHaveText("Не настроен");
  await expect(saveCaptainButton).toBeEnabled();
  await expect(cancelCaptainButton).toBeEnabled();
  await saveCaptainButton.click();
  await expect(firstSlotBadge).toHaveText("Настроен");
  await expect(saveCaptainButton).toBeDisabled();
  const roleOnlyRequestBody = requestBody as { slots: Array<{ slot_number: number; allowed_roles: string[]; desired_heroes: string[] }> } | null;
  expect(roleOnlyRequestBody?.slots[0]).toMatchObject({
    slot_number: 1,
    allowed_roles: ["Carry"],
    desired_heroes: []
  });
  expect(captainProfileBody).toMatchObject({
    captain_team_name: "SmokeTeam"
  });

  const supportButton = firstSlotCard.getByRole("button", { name: "Support", exact: true });
  await supportButton.click();
  await expect(firstSlotBadge).toHaveText("Не настроен");
  await expect(cancelCaptainButton).toBeEnabled();
  await cancelCaptainButton.click();
  await expect(firstSlotBadge).toHaveText("Настроен");
  await expect(carryButton).toHaveClass(/active/);
  await expect(supportButton).not.toHaveClass(/active/);
  await expect(saveCaptainButton).toBeDisabled();
  await expect(cancelCaptainButton).toBeDisabled();

  const emptyHeroesBox = await firstSlotHeroes.boundingBox();
  expect(emptyHeroesBox?.height).toBeGreaterThanOrEqual(52);
  await expect(firstSlotHeroes).toContainText("Герои не выбраны.");
  await expect(firstSlotToggle).toHaveText("Выбрать героев");

  await firstSlotToggle.click();
  const firstSlotPicker = page.getByTestId("profile-dream-slot-1-hero-picker");
  await expect(firstSlotPicker).toBeVisible();
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(1);
  const expectedRowEndSlot = await page.locator(".teammate-grid").evaluate((grid) => {
    const columns = window.getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length;
    return Math.max(1, Math.min(columns, 3));
  });
  expect(await firstSlotPicker.evaluate((node, slotNumber) => (
    node.previousElementSibling?.textContent?.includes(`Тиммейт ${slotNumber}`)
  ), expectedRowEndSlot)).toBe(true);
  const teammateGridBox = await page.locator(".teammate-grid").boundingBox();
  const pickerBox = await firstSlotPicker.boundingBox();
  expect(pickerBox?.width).toBeGreaterThanOrEqual((teammateGridBox?.width ?? 0) - 2);
  const abramsDreamHero = page.getByTestId("profile-dream-slot-1-hero-abrams");
  await abramsDreamHero.click();
  await expect(abramsDreamHero).toHaveClass(/selected/);
  expect(await abramsDreamHero.evaluate((element) => getComputedStyle(element, "::after").content)).toBe('"✓"');
  await expect(firstSlotBadge).toHaveText("Не настроен");
  await saveCaptainButton.click();
  await expect(saveCaptainButton).toHaveText("Сохранено");
  await expect(firstSlotBadge).toHaveText("Настроен");
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(0);
  expect(requestCount).toBe(2);
  const firstHeroRequestBody = requestBody as { slots: Array<{ desired_heroes: string[] }> } | null;
  expect(firstHeroRequestBody?.slots[0]).toMatchObject({ desired_heroes: ["Abrams"] });

  await firstSlotToggle.click();
  await expect(firstSlotPicker).toBeVisible();
  await page.getByTestId("profile-dream-slot-1-hero-apollo").click();
  await page.getByTestId("profile-dream-slot-1-hero-bebop").click();
  await expect(page.getByTestId("profile-dream-slot-1-hero-billy")).toBeEnabled();
  await page.getByTestId("profile-dream-slot-1-hero-billy").click();
  await page.getByTestId("profile-dream-slot-1-hero-calico").click();
  await expect(page.getByTestId("profile-dream-slot-1-hero-dynamo")).toBeDisabled();
  await expect(firstSlotToggle).toHaveText("Выбрать героев");

  await saveCaptainButton.click();
  await expect(saveCaptainButton).toHaveText("Сохранено");
  await expect(saveCaptainButton).toBeDisabled();
  await expect(cancelCaptainButton).toBeDisabled();
  await expect(firstSlotBadge).toHaveText("Настроен");
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(0);
  await expect(page.locator(".captain-profile-actions")).toHaveCSS("align-items", "center");
  expect(requestCount).toBe(3);
  const savedRequestBody = requestBody as { slots: Array<{ slot_number: number; allowed_roles: string[]; desired_heroes: string[] }> } | null;
  expect(savedRequestBody?.slots[0]).toMatchObject({
    slot_number: 1,
    allowed_roles: ["Carry"],
    desired_heroes: ["Abrams", "Apollo", "Bebop", "Billy", "Calico"]
  });
  if ((page.viewportSize()?.width ?? 0) <= 820) {
    const heroPositions = await firstSlotHeroes.locator(".dream-selected-hero").evaluateAll((heroes) => (
      heroes.map((hero) => ({
        left: hero.getBoundingClientRect().left,
        top: hero.getBoundingClientRect().top,
        width: hero.getBoundingClientRect().width
      }))
    ));
    expect(heroPositions).toHaveLength(5);
    expect(Math.max(...heroPositions.slice(0, 3).map(({ top }) => top))
      - Math.min(...heroPositions.slice(0, 3).map(({ top }) => top))).toBeLessThanOrEqual(1);
    expect(heroPositions[3].top).toBeGreaterThan(heroPositions[0].top);
    expect(new Set(heroPositions.slice(0, 3).map(({ width }) => Math.round(width))).size).toBe(1);
  }
});
