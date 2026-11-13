import { expect, test } from "@playwright/test";
import { appearanceCss, appearanceDefaults, appearanceGroups, resolveAppearanceColors } from "../../lib/appearance-palette";

test("site palette saves to visitor SSR, remains recoverable after reload, and can be reset", async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== "appearance-recovery", "The shared public palette fixture is mutated only after responsive projects complete.");
  let editable = { ...appearanceDefaults };
  let revision = 1;
  const initialFixtureResponse = await request.put("http://127.0.0.1:3198/__test/appearance", {
    data: { colors: resolveAppearanceColors(editable), revision, groups: appearanceGroups }
  });
  expect(initialFixtureResponse.ok()).toBe(true);
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-appearance-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.route("**/api/v1/admin/overview", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ users_total: 1, tournaments_total: 0, tournaments_attention_total: 0, audit_events_total: 0 }) }));
  await page.route("**/api/v1/admin/users**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/audit-logs**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/preprod-test-runs**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/tournaments**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "X-Total-Count": "0", "X-Limit": "25", "X-Offset": "0", "X-Has-More": "false" },
    body: "[]"
  }));
  await page.route("**/api/v1/admin/appearance", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as { colors: typeof editable; expected_revision: number };
      expect(body.expected_revision).toBe(revision);
      await new Promise((resolve) => setTimeout(resolve, 250));
      editable = body.colors;
      revision += 1;
      const fixtureResponse = await request.put("http://127.0.0.1:3198/__test/appearance", {
        data: { colors: resolveAppearanceColors(editable), revision, groups: appearanceGroups }
      });
      expect(fixtureResponse.ok()).toBe(true);
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ colors: resolveAppearanceColors(editable), revision, groups: appearanceGroups })
    });
  });

  try {
  await page.goto("/platform-ops");
  await reloadUntilTheme(page, "--ui-canvas", resolveAppearanceColors(appearanceDefaults).background);
  await reloadUntilTheme(page, "--ui-text", resolveAppearanceColors(appearanceDefaults).foreground);
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: "Цвета сайта" }).click();
  const appearance = page.getByTestId("admin-appearance-page");
  await expect(appearance).toBeVisible();
  const editorScreenshot = testInfo.outputPath(`admin-appearance-${testInfo.project.name}.png`);
  await waitForVisuals(page);
  await page.screenshot({ path: editorScreenshot, fullPage: true, animations: "disabled" });
  await testInfo.attach(`admin-appearance-${testInfo.project.name}`, {
    path: editorScreenshot,
    contentType: "image/png"
  });
  const backgroundHue = appearance.getByLabel("Тон цвета «Фон»");
  const backgroundSaturation = appearance.getByLabel("Насыщенность цвета «Фон»");
  const backgroundBrightness = appearance.getByLabel("Яркость цвета «Фон»");
  await backgroundHue.focus();
  await backgroundHue.press("ArrowRight");
  await appearance.getByLabel("Точный HEX цвета «Фон»").fill("F1E9D8");
  let expectedHsv = hsv("#F1E9D8");
  await expect(backgroundHue).toHaveValue(String(expectedHsv.h));
  await expect(backgroundSaturation).toHaveValue(String(expectedHsv.s));
  await expect(backgroundBrightness).toHaveValue(String(expectedHsv.v));
  await appearance.getByLabel("Фон", { exact: true }).evaluate((element) => {
    const input = element as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setValue?.call(input, "#2040E0");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expectedHsv = hsv("#2040E0");
  await expect(backgroundHue).toHaveValue(String(expectedHsv.h));
  await expect(backgroundSaturation).toHaveValue(String(expectedHsv.s));
  await expect(backgroundBrightness).toHaveValue(String(expectedHsv.v));
  const pickerHsv = expectedHsv;
  const pickerHex = await appearance.getByLabel("Точный HEX цвета «Фон»").inputValue();
  await backgroundHue.focus();
  await backgroundHue.press("ArrowRight");
  const adjustedHex = await appearance.getByLabel("Точный HEX цвета «Фон»").inputValue();
  expect(adjustedHex).not.toBe(pickerHex);
  expectedHsv = hsv(adjustedHex);
  await expect(backgroundHue).toHaveValue(String(expectedHsv.h));
  await expect(backgroundSaturation).toHaveValue(String(pickerHsv.s));
  await expect(backgroundBrightness).toHaveValue(String(pickerHsv.v));
  await appearance.getByLabel("Точный HEX цвета «Фон»").fill("F1E9D8");
  await appearance.getByLabel("Точный HEX цвета «Основной текст»").fill("#F1E9D8");
  await expect(appearance.getByRole("status").filter({ hasText: "Контраст основного текста" })).toBeVisible();
  const backgroundHex = appearance.getByLabel("Точный HEX цвета «Фон»");
  await backgroundHex.fill("12345");
  await expect(backgroundHex).toHaveAttribute("aria-invalid", "true");
  await expect(appearance.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  await backgroundHex.fill("#F1E9D8");
  const saveButton = appearance.getByRole("button", { name: "Сохранить" });
  await saveButton.click();
  await expect(backgroundHex).toBeDisabled();
  await expect(appearance.getByText("Новая палитра сохранена для сайта.")).toBeVisible();

  await reloadUntilTheme(page, "--ui-canvas", "#F1E9D8");
  const navigation = page.getByLabel("Навигация админ-панели");
  await expect(navigation.getByRole("button", { name: "Цвета сайта" })).toBeVisible();
  await expect.poll(() => navigation.evaluate((element) => getComputedStyle(element.closest(".ops-shell")!).getPropertyValue("--ui-text").trim())).toBe("#F7F8FC");
  await navigation.getByRole("button", { name: "Цвета сайта" }).click();
  const reloadedAppearance = page.getByTestId("admin-appearance-page");
  const reloadedBackground = reloadedAppearance.getByLabel("Точный HEX цвета «Фон»");
  await expect(reloadedBackground).toHaveValue("#F1E9D8");
  await expect(page.locator(":root")).toHaveCSS("--ui-text", "#F1E9D8");
  await page.context().clearCookies();
  await page.goto("/");
  await reloadUntilTheme(page, "--ui-canvas", "#F1E9D8");

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-appearance-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: "Цвета сайта" }).click();
  const resetAppearance = page.getByTestId("admin-appearance-page");
  const resetBackground = resetAppearance.getByLabel("Точный HEX цвета «Фон»");
  await expect(resetBackground).toHaveValue("#F1E9D8");
  await resetBackground.fill("#F1E9D9");
  const resetButton = resetAppearance.getByRole("button", { name: "Стандартные цвета" });
  await expect(resetButton).toBeEnabled();
  await resetButton.click();
  await resetAppearance.getByRole("button", { name: "Сохранить" }).click();
  await expect.poll(() => editable).toEqual({ ...appearanceDefaults });
  await page.context().clearCookies();
  await page.goto("/");
  await reloadUntilTheme(page, "--ui-canvas", resolveAppearanceColors(appearanceDefaults).background);
  } finally {
    editable = { ...appearanceDefaults };
    revision += 1;
    const fixtureResponse = await request.put("http://127.0.0.1:3198/__test/appearance", {
      data: { colors: resolveAppearanceColors(editable), revision, groups: appearanceGroups }
    });
    expect(fixtureResponse.ok()).toBe(true);
  }
});

test("site logo and appearance editor follow the palette across viewports", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "appearance-recovery", "Visual preview runs in each responsive project; recovery owns the shared API fixture.");
  await page.goto("/");
  const darkColors = resolveAppearanceColors(appearanceDefaults);
  await setLocalAppearance(page, darkColors);
  await expect(page.locator(":root")).toHaveCSS("--ui-canvas", darkColors.background);
  await expect(page.locator(":root")).toHaveCSS("--ui-text", darkColors.foreground);
  await expect(page.locator(":root")).toHaveCSS("--ui-text-muted", darkColors.foregroundMuted);
  const mark = page.locator(".site-header .brand-mark");
  await expect(mark).toBeVisible();
  await expect(mark).toHaveCSS("mask-image", /old-sparky-arena-logo-v3\.webp/);
  await expect(mark).toHaveCSS("background-color", await themeColor(page, "--ui-text"));
  await expectThemeTextContrast(page);

  const darkScreenshot = testInfo.outputPath(`home-dark-${testInfo.project.name}.png`);
  await waitForVisuals(page);
  await page.screenshot({ path: darkScreenshot, fullPage: true, animations: "disabled" });
  await testInfo.attach(`home-dark-${testInfo.project.name}`, { path: darkScreenshot, contentType: "image/png" });

  const lightColors = resolveAppearanceColors({ ...appearanceDefaults, background: "#F1E9D8", foreground: "#000000" });
  await setLocalAppearance(page, lightColors);
  await expect(page.locator(":root")).toHaveCSS("--ui-canvas", lightColors.background);
  await expect(page.locator(":root")).toHaveCSS("--ui-text", lightColors.foreground);
  await expect(page.locator(":root")).toHaveCSS("--ui-text-muted", lightColors.foregroundMuted);
  await expect(mark).toHaveCSS("background-color", await themeColor(page, "--ui-text"));
  await expectThemeTextContrast(page);
  const lightScreenshot = testInfo.outputPath(`home-light-${testInfo.project.name}.png`);
  await waitForVisuals(page);
  await page.screenshot({ path: lightScreenshot, fullPage: true, animations: "disabled" });
  await testInfo.attach(`home-light-${testInfo.project.name}`, { path: lightScreenshot, contentType: "image/png" });

  await mockAppearanceEditorRequests(page);
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-appearance-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: "Цвета сайта" }).click();
  const appearance = page.getByTestId("admin-appearance-page");
  await expect(appearance).toBeVisible();
  await waitForVisuals(page);
  const editorScreenshot = testInfo.outputPath(`admin-appearance-${testInfo.project.name}.png`);
  await page.screenshot({ path: editorScreenshot, fullPage: true, animations: "disabled" });
  await testInfo.attach(`admin-appearance-${testInfo.project.name}`, { path: editorScreenshot, contentType: "image/png" });
});

async function setLocalAppearance(page: import("@playwright/test").Page, colors: Record<string, string>) {
  await page.evaluate((css) => {
    let style = document.querySelector<HTMLStyleElement>("style[data-appearance-test-preview]");
    if (!style) {
      style = document.createElement("style");
      style.dataset.appearanceTestPreview = "true";
      const nonce = document.querySelector<HTMLStyleElement>("style[nonce]")?.nonce;
      if (nonce) style.nonce = nonce;
      document.head.append(style);
    }
    style.textContent = css;
  }, appearanceCss(colors));
}

async function mockAppearanceEditorRequests(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/admin/overview", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ users_total: 1, tournaments_total: 0, tournaments_attention_total: 0, audit_events_total: 0 }) }));
  await page.route("**/api/v1/admin/users**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/audit-logs**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/preprod-test-runs**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/v1/admin/tournaments**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "X-Total-Count": "0", "X-Limit": "25", "X-Offset": "0", "X-Has-More": "false" },
    body: "[]"
  }));
  const colors = resolveAppearanceColors(appearanceDefaults);
  await page.route("**/api/v1/admin/appearance", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ colors, revision: 1, groups: appearanceGroups })
  }));
}

async function themeColor(page: import("@playwright/test").Page, role: string) {
  return page.evaluate((tokenName) => {
    const token = getComputedStyle(document.documentElement).getPropertyValue(tokenName).trim();
    const probe = document.createElement("span");
    probe.style.color = token;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, role);
}

async function expectThemeTextContrast(page: import("@playwright/test").Page) {
  await settleThemeText(page);
  const metrics = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const resolve = (token: string) => {
      const probe = document.createElement("span");
      probe.style.color = root.getPropertyValue(token).trim();
      document.body.append(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    };
    const rgb = (value: string) => {
      const channels = value.match(/[\d.]+/gu)?.slice(0, 3).map(Number);
      if (!channels || channels.length !== 3) throw new Error(`Unsupported computed color: ${value}`);
      return channels;
    };
    const luminance = (value: string) => {
      const [red, green, blue] = rgb(value).map((channel) => channel / 255)
        .map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
      return red * 0.2126 + green * 0.7152 + blue * 0.0722;
    };
    const contrast = (first: string, second: string) => {
      const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
      return (lighter + 0.05) / (darker + 0.05);
    };
    const color = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) throw new Error(`Missing visual acceptance element: ${selector}`);
      return getComputedStyle(element).color;
    };
    const muted = resolve("--ui-text-muted");
    const soft = resolve("--ui-text-soft");
    const canvas = resolve("--ui-canvas");
    const deep = resolve("--ui-canvas-deep");
    const surface = resolve("--ui-surface-strong");
    const nav = color(".site-header .nav-link:not(.nav-link-active)");
    const activeNav = color(".site-header .nav-link-active");
    const subtitle = color(".hero-wrap-home .hero-subtitle");
    const eyebrow = color(".home-section-eyebrow");
    const footer = color(".site-footer-nav a");
    return {
      navMatchesRole: nav === muted,
      muted,
      navColor: nav,
      subtitleMatchesRole: subtitle === soft,
      eyebrowMatchesRole: eyebrow === muted,
      footerMatchesRole: footer === muted,
      navContrast: contrast(nav, deep),
      activeNavContrast: contrast(activeNav, deep),
      subtitleContrast: contrast(subtitle, surface),
      eyebrowContrast: contrast(eyebrow, canvas),
      footerContrast: contrast(footer, surface)
    };
  });
  expect(metrics.navMatchesRole, `normal nav color ${metrics.navColor} should match muted ${metrics.muted}`).toBe(true);
  expect(metrics.subtitleMatchesRole).toBe(true);
  expect(metrics.eyebrowMatchesRole).toBe(true);
  expect(metrics.footerMatchesRole).toBe(true);
  expect(metrics.navContrast).toBeGreaterThanOrEqual(4.5);
  expect(metrics.activeNavContrast).toBeGreaterThanOrEqual(4.5);
  expect(metrics.subtitleContrast).toBeGreaterThanOrEqual(4.5);
  expect(metrics.eyebrowContrast).toBeGreaterThanOrEqual(4.5);
  expect(metrics.footerContrast).toBeGreaterThanOrEqual(4.5);
}

async function settleThemeText(page: import("@playwright/test").Page) {
  const selectors = [
    [".site-header .nav-link:not(.nav-link-active)", "--ui-text-muted"],
    [".hero-wrap-home .hero-subtitle", "--ui-text-soft"],
    [".home-section-eyebrow", "--ui-text-muted"],
    [".site-footer-nav a", "--ui-text-muted"]
  ] as const;
  for (const [selector, role] of selectors) {
    await expect.poll(
      () => page.locator(selector).first().evaluate((element) => getComputedStyle(element).color),
      { timeout: 5_000, intervals: [50, 100, 200] }
    ).toBe(await themeColor(page, role));
  }
}

async function waitForVisuals(page: import("@playwright/test").Page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(Array.from(document.images, (image) => image.decode().catch(() => undefined)));
  });
}

async function reloadUntilTheme(page: import("@playwright/test").Page, role: string, expected: string) {
  await expect.poll(async () => {
    await page.reload();
    return page.locator(":root").evaluate((root, tokenName) => (
      getComputedStyle(root).getPropertyValue(tokenName).trim()
    ), role);
  }, { timeout: 5_000, intervals: [100, 250, 500] }).toBe(expected);
}

function hsv(hex: string) {
  const r = Number.parseInt(hex.slice(1, 3), 16) / 255;
  const g = Number.parseInt(hex.slice(3, 5), 16) / 255;
  const b = Number.parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b); const min = Math.min(r, g, b); const delta = max - min;
  const h = !delta ? 0 : max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return { h: Math.round((h * 60 + 360) % 360), s: max ? Math.round(delta / max * 100) : 0, v: Math.round(max * 100) };
}
