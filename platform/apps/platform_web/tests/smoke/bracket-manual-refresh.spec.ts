import { expect, test } from "@playwright/test";

test("bracket stays request-driven until the page is manually reloaded", async ({ page }) => {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    }
  ]);

  const forbiddenBackgroundRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      request.resourceType() === "eventsource"
      || url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket"
    ) {
      forbiddenBackgroundRequests.push(`${request.method()} ${url.pathname}`);
    }
  });

  await page.goto("/tournaments/night-veil-open-5/bracket");
  await expect(page.locator("[data-testid='bracket-shell'], [data-testid='bracket-empty']")).toHaveCount(1);

  await page.waitForTimeout(4_000);
  expect(forbiddenBackgroundRequests).toEqual([]);

  await page.reload();
  await expect(page.locator("[data-testid='bracket-shell'], [data-testid='bracket-empty']")).toHaveCount(1);
  expect(forbiddenBackgroundRequests).toEqual([]);
});
