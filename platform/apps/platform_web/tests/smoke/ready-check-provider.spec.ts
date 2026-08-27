import { expect, test } from "@playwright/test";

const sessionCookie = {
  name: "deadlock_platform_session",
  value: "ready-check-provider-session",
  url: "http://127.0.0.1:3100",
};

test("keeps the initial active button when the Ready Check projection is unavailable", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:35:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-polling-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const stateRequests: string[] = [];
  const streamRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/ready-check/state") {
      stateRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  const readyButton = page.getByRole("button", { name: "Подтвердить участие" });
  await expect(readyButton).toBeEnabled();
  expect(streamRequests).toHaveLength(0);
  await expect.poll(() => stateRequests.length, { timeout: 5_000 }).toBeGreaterThan(0);
  await expect(readyButton).toBeEnabled();
});

test("does not poll at T while the Ready Check SSE is established", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-sse-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const stateRequests: string[] = [];
  const streamRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/ready-check/state") {
      stateRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(1);
  await page.clock.fastForward(12_000);

  expect(stateRequests).toHaveLength(0);
});

test("refreshes the stream proof before a bounded multi-check horizon expires", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:35:00Z") });
  const proofRefreshScope = `${test.info().project.name}-${test.info().retry}`;
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-proof-refresh-smoke",
      value: proofRefreshScope,
      url: "http://127.0.0.1:3100",
    },
  ]);

  let agendaRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/agenda") {
      agendaRequests += 1;
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => agendaRequests).toBe(1);
  await page.clock.fastForward(9 * 60_000 + 1);
  await expect.poll(() => agendaRequests, { timeout: 5_000 }).toBe(2);
});

test("closes the stream between Ready Checks and reopens at the next admission window", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-future-gap-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const streamRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(1);
  await page.goto("/tournaments/citadel-clash-3");
  await page.clock.fastForward(10 * 60_000);
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(2);
});

test("uses authoritative state recovery for a relay resync and never activates from the hint", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-resync-recovery-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  let stateRequests = 0;
  let streamRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/ready-check/state") {
      stateRequests += 1;
    }
    if (url.pathname === "/api/v1/ready-check/events") {
      streamRequests += 1;
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests, { timeout: 5_000 }).toBe(1);
  await expect.poll(() => stateRequests, { timeout: 5_000 }).toBe(1);
  await expect(page.getByRole("button", { name: "Подтвердить участие" })).toBeEnabled();
});

test("does not activate the button when relay resync state remains waiting", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-resync-no-activation-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  let stateRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/state") {
      stateRequests += 1;
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => stateRequests, { timeout: 5_000 }).toBe(1);
  await expect(page.getByRole("button", { name: "Подтвердить участие" })).toBeDisabled();
});

test("opens Ready Check SSE only for the visible matching tournament detail", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-sse-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const streamRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });

  await page.goto("/profile/me");
  expect(streamRequests).toHaveLength(0);
  await page.goto("/");
  expect(streamRequests).toHaveLength(0);
  await page.goto("/tournaments/citadel-clash-3");
  expect(streamRequests).toHaveLength(0);

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(1);
  await page.goto("/profile/me");
  await page.clock.runFor(30_000);
  expect(streamRequests).toHaveLength(1);
});

test("releases the page-scoped stream when the tab becomes hidden", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:29:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-sse-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const streamRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });
  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(1);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(30_000);
  expect(streamRequests).toHaveLength(1);
});

test("falls back after a bounded SSE handshake timeout and jitters recovery", async ({ page }) => {
  await page.addInitScript(() => {
    Math.random = () => 0;
  });
  await page.clock.install({ time: new Date("2026-06-07T15:35:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-stalled-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  const stateRequests: string[] = [];
  const streamRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/ready-check/state") {
      stateRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/ready-check/events") {
      streamRequests.push(request.url());
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(1);
  await page.clock.fastForward(3_000);
  expect(stateRequests).toHaveLength(0);

  await page.clock.fastForward(59_000);
  expect(streamRequests).toHaveLength(1);
  await page.clock.fastForward(1_001);
  await expect.poll(() => streamRequests.length, { timeout: 5_000 }).toBe(2);
});

test("does not reload the PostgreSQL-backed agenda on soft navigation", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-06-07T15:35:00Z") });
  await page.context().addCookies([
    sessionCookie,
    {
      name: "ready-check-provider-polling-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);

  let agendaRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/ready-check/agenda") {
      agendaRequests += 1;
    }
  });

  await page.goto("/tournaments/night-veil-open-5");
  await expect.poll(() => agendaRequests).toBe(1);
  await page.getByRole("link", { name: "Профиль: lisalexy" }).click();
  await expect(page).toHaveURL(/\/profile\/me$/);

  expect(agendaRequests).toBe(1);
});
