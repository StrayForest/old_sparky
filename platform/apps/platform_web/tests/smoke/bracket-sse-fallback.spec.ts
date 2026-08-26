import { expect, test } from "@playwright/test";

test("abandons a stalled SSE handshake and starts bracket polling", async ({ page }) => {
  const configuredTimeoutMs = Number(process.env.NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS ?? "5000");
  const fallbackTimeoutMs = Number.isFinite(configuredTimeoutMs)
    ? Math.min(30_000, Math.max(500, Math.round(configuredTimeoutMs)))
    : 5_000;
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    }
  ]);

  let sseAttempts = 0;
  const bracketPollingRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket"
      && request.method() === "GET"
    ) {
      bracketPollingRequests.push(request.headers()["if-none-match"] ?? "");
    }
  });
  await page.route(
    "**/api/v1/tournaments/night-veil-open-5/bracket/events",
    async (route) => {
      sseAttempts += 1;
      await new Promise((resolve) => setTimeout(resolve, 15_000));
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: ": connected\n\n"
      });
    },
  );

  await page.goto("/tournaments/night-veil-open-5/bracket");
  await expect.poll(() => sseAttempts, { timeout: 2_000 }).toBe(1);
  await expect.poll(() => bracketPollingRequests.length, { timeout: fallbackTimeoutMs + 4_000 })
    .toBeGreaterThan(0);

  const conditional = await page.request.get(
    "http://127.0.0.1:3100/api/v1/tournaments/night-veil-open-5/bracket?teams_view=summary",
    { headers: { "if-none-match": 'W/"mock-bracket-revision-0"' } },
  );
  expect(conditional.status()).toBe(304);
});
