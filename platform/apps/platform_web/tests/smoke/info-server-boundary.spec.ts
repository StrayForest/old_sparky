import { expect, test } from "@playwright/test";

test("info static sections render in initial HTML without a browser support-status fetch", async ({ page, request }) => {
  const response = await request.get("/info");
  expect(response.ok()).toBe(true);
  const html = await response.text();
  expect(html).toContain("Как пользоваться");
  expect(html).toContain("Частые вопросы");
  expect(html).toContain("Поддержка");

  const browserStatusRequests: string[] = [];
  page.on("request", (browserRequest) => {
    if (new URL(browserRequest.url()).pathname === "/api/v1/content/support/status") {
      browserStatusRequests.push(browserRequest.url());
    }
  });

  await page.goto("/info");
  await expect(page.getByRole("heading", { name: "Информация", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Как пользоваться", exact: true })).toBeVisible();
  expect(browserStatusRequests).toEqual([]);
});

test("support submission omits the session cookie and does not bootstrap CSRF", async ({ page }) => {
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "smoke-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.goto("/info");

  const csrfRequests: string[] = [];
  const supportRequestHeaders: Array<Record<string, string>> = [];
  page.on("request", (browserRequest) => {
    const path = new URL(browserRequest.url()).pathname;
    if (path === "/api/v1/auth/csrf") {
      csrfRequests.push(browserRequest.url());
    }
    if (path === "/api/v1/content/support/messages") {
      supportRequestHeaders.push(browserRequest.headers());
    }
  });

  await page.getByLabel("Имя или ник").fill("Smoke Player");
  await page.getByLabel("Почта для ответа").fill("smoke@example.test");
  await page.getByLabel("Сообщение").fill("Проверка публичной формы поддержки без сессионных credentials.");
  await page.locator("form.support-form").evaluate((form) => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });

  await expect.poll(() => supportRequestHeaders.length).toBe(1);
  expect(csrfRequests).toEqual([]);
  expect(supportRequestHeaders[0].cookie).toBeUndefined();
  expect(supportRequestHeaders[0]["x-csrf-token"]).toBeUndefined();
});
