import { expect, test } from "@playwright/test";

test("abandons a stalled SSE handshake and starts bracket polling", async ({ page }) => {
  const configuredTimeoutMs = Number(process.env.NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS ?? "1000");
  const fallbackTimeoutMs = Number.isFinite(configuredTimeoutMs)
    ? Math.min(30_000, Math.max(500, Math.round(configuredTimeoutMs)))
    : 1_000;
  // Page routes do not intercept a SharedWorker's EventSource. Keep this
  // fallback test focused on the direct browser connection it controls.
  await page.addInitScript(() => {
    Object.defineProperty(window, "SharedWorker", {
      configurable: true,
      value: undefined,
    });
  });
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

test("returns to SSE through a full-jitter recovery window after fallback", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "SharedWorker", {
      configurable: true,
      value: undefined,
    });
    Math.random = () => 0;
  });
  await page.clock.install({ time: new Date("2026-08-27T12:00:00Z") });
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
      bracketPollingRequests.push(request.url());
    }
  });
  await page.route(
    "**/api/v1/tournaments/night-veil-open-5/bracket/events",
    async (route) => {
      sseAttempts += 1;
      if (sseAttempts === 1) {
        await route.fulfill({
          status: 200,
          headers: { "content-type": "text/event-stream" },
          body: "event: connected\ndata: {}\n\n"
        });
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 15_000));
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: ": connected\n\n"
      });
    },
  );

  await page.goto("/tournaments/night-veil-open-5/bracket");
  await expect.poll(() => sseAttempts, { timeout: 5_000 }).toBe(1);
  await expect.poll(() => bracketPollingRequests.length, { timeout: 5_000 })
    .toBeGreaterThan(0);

  await page.clock.fastForward(59_999);
  expect(sseAttempts).toBe(1);
  await page.clock.fastForward(1_001);
  await expect.poll(() => sseAttempts, { timeout: 5_000 }).toBe(2);
});
