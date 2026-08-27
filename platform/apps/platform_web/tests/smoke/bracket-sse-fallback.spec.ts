import { expect, test } from "@playwright/test";

test("uses a cheap bracket revision probe instead of bracket SSE", async ({ page }) => {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    }
  ]);

  let sseAttempts = 0;
  const bracketRequests: string[] = [];
  const probeRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket"
      && request.method() === "GET"
    ) {
      bracketRequests.push(request.url());
    }
    if (
      url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket/probe"
      && request.method() === "GET"
    ) {
      probeRequests.push(request.url());
    }
  });
  await page.route(
    "**/api/v1/tournaments/night-veil-open-5/bracket/events",
    async (route) => {
      sseAttempts += 1;
      await route.abort();
    },
  );

  await page.goto("/tournaments/night-veil-open-5/bracket");
  await expect.poll(() => sseAttempts, { timeout: 4_000 }).toBe(0);
  await expect.poll(() => probeRequests.length, { timeout: 8_000 }).toBeGreaterThan(0);

  const conditional = await page.request.get(
    "http://127.0.0.1:3100/api/v1/tournaments/night-veil-open-5/bracket?teams_view=summary",
    { headers: { "if-none-match": 'W/"mock-bracket-revision-0"' } },
  );
  expect(conditional.status()).toBe(304);
});

test("reloads the full bracket only after the revision changes", async ({ page }) => {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    }
  ]);

  let probeRevision = 0;
  const bracketRequests: string[] = [];
  const probeRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket"
      && request.method() === "GET"
    ) {
      bracketRequests.push(request.url());
    }
    if (
      url.pathname === "/api/v1/tournaments/night-veil-open-5/bracket/probe"
      && request.method() === "GET"
    ) {
      probeRequests.push(request.url());
    }
  });
  await page.route("**/api/v1/tournaments/night-veil-open-5/bracket/probe**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ revision: probeRevision, status: "ready" }),
    });
  });
  await page.route("**/api/v1/tournaments/night-veil-open-5/bracket?*", async (route) => {
    const response = await route.fetch();
    const payload = await response.json() as { revision?: number };
    await route.fulfill({
      response,
      body: JSON.stringify({ ...payload, revision: probeRevision }),
    });
  });

  await page.goto("/tournaments/night-veil-open-5/bracket");
  await expect.poll(() => probeRequests.length, { timeout: 8_000 }).toBeGreaterThan(0);
  const initialBracketRequests = bracketRequests.length;

  probeRevision = 1;
  await expect.poll(() => probeRequests.length, { timeout: 8_000 }).toBeGreaterThan(1);
  await expect.poll(() => bracketRequests.length, { timeout: 5_000 })
    .toBeGreaterThan(initialBracketRequests);

  const afterRevisionRequests = bracketRequests.length;
  await expect.poll(() => probeRequests.length, { timeout: 8_000 }).toBeGreaterThan(2);
  expect(bracketRequests.length).toBe(afterRevisionRequests);
});
