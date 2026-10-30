import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

type SsrPlanFixture = {
  run_id: string;
  request_path: string;
  off_start_ms: number;
  off_end_ms: number;
  selector_matched_calls: number;
  selector_accepted: boolean;
};

async function requestCount(scope: string, path: string): Promise<number> {
  const response = await fetch(
    `http://127.0.0.1:3199/__test/request-count?scope=${encodeURIComponent(scope)}&path=${encodeURIComponent(path)}`,
  );
  expect(response.ok).toBe(true);
  const result = await response.json() as { count: number };
  return result.count;
}

test("actual Next plan and Node guard return the trusted SSR request ID", async ({ page }, testInfo) => {
  const fixtureRoot = process.env.PLATFORM_SSR_DIAGNOSTIC_FIXTURE_ROOT;
  expect(fixtureRoot).toBeTruthy();
  const fixture = JSON.parse(
    readFileSync(join(fixtureRoot!, "fixture.json"), "utf8"),
  ) as SsrPlanFixture;
  expect(fixture.run_id).toMatch(/^[0-9a-f]{32}$/u);
  expect(fixture.request_path).toBe("/tournaments/night-veil-open-5");

  const requestId = process.env.PLATFORM_SSR_DIAGNOSTIC_REQUEST_ID ?? "";
  expect(requestId).toMatch(/^[0-9a-f]{32}$/u);
  const countScope = `ssr-e2e-${testInfo.workerIndex}-${requestId}`;
  const reset = await page.request.post(
    "http://127.0.0.1:3199/__test/request-count/reset",
    { params: { scope: countScope } },
  );
  expect(reset.ok()).toBe(true);
  await page.context().addCookies([
    { name: "ssr-bootstrap-profile-smoke", value: "1", url: "http://127.0.0.1:3100" },
    { name: "ssr-count-scope", value: countScope, url: "http://127.0.0.1:3100" },
  ]);

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.isNavigationRequest()
        && request.resourceType() === "document"
        && url.pathname === fixture.request_path) {
      await route.continue({
        headers: {
          ...(await request.allHeaders()),
          "x-request-id": requestId,
          "x-platform-ssr-trace": fixture.run_id,
          "x-platform-ssr-request-id": "forged-request-id",
        },
      });
      return;
    }
    await route.continue();
  });

  const untilOffStartMs = fixture.off_start_ms - Date.now() + 25;
  if (untilOffStartMs > 0) await page.waitForTimeout(untilOffStartMs);
  expect(Date.now()).toBeGreaterThanOrEqual(fixture.off_start_ms);
  expect(Date.now()).toBeLessThan(fixture.off_end_ms);

  const response = await page.goto(fixture.request_path, {
    waitUntil: "domcontentloaded",
  });
  expect(response).not.toBeNull();
  expect(response?.status()).toBe(200);
  const selection = JSON.parse(
    readFileSync(join(fixtureRoot!, "fixture.json"), "utf8"),
  ) as SsrPlanFixture;
  expect(selection.selector_matched_calls).toBeGreaterThanOrEqual(1);
  expect(selection.selector_accepted).toBe(true);
  expect(response?.headers()["x-platform-ssr-request-id"]).toBe(requestId);
  expect(response?.headers()["x-platform-ssr-request-id"]).not.toBe("forged-request-id");
  await expect(page.getByRole("heading", { name: "Night Veil Open #5", level: 1 })).toBeVisible();
  await expect.poll(
    () => requestCount(countScope, "/api/v1/tournaments/night-veil-open-5/workspace"),
  ).toBe(1);
});
