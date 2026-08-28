import { expect, test, type Page } from "@playwright/test";

const sessionCookie = {
  name: "deadlock_platform_session",
  value: "ready-check-timer-session",
  url: "http://127.0.0.1:3100",
};

function isReadyCheckRequest(pathname: string) {
  return pathname.endsWith("/deadlock/ready-check")
    || pathname.endsWith("/deadlock/ready-check/vote");
}

async function preparePage(page: Page, time: string, mode: string) {
  await page.clock.install({ time: new Date(time) });
  await page.context().addCookies([
    sessionCookie,
    {
      name: mode,
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);
}

function trackReadyCheckRequests(page: Page) {
  const requests: string[] = [];
  page.on("request", (request) => {
    if (isReadyCheckRequest(new URL(request.url()).pathname)) {
      requests.push(request.url());
    }
  });
  return requests;
}

function readyButton(page: Page) {
  return page.getByRole("main").getByRole("button", { name: "Подтвердить участие" });
}

async function waitForReadyCheckTimer(page: Page, phase: string) {
  const step = page.getByTestId("ready-check-step");
  await expect(step).toHaveAttribute("data-ready-check-timer-mounted", "true");
  await expect(step).toHaveAttribute("data-ready-check-phase", phase);
}

test("waits before starts_at and activates locally at starts_at without a request", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-before-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "waiting");
  await expect(readyButton(page)).toBeDisabled();
  expect(requests).toHaveLength(0);

  await page.clock.runFor(60_000);
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("crosses the exact starts_at boundary locally", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-at-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "waiting");
  await expect(readyButton(page)).toBeDisabled();
  await page.clock.runFor(59_000);
  await expect(readyButton(page)).toBeDisabled();
  await page.clock.runFor(1_000);
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("loads after starts_at with an active button and no Ready Check request", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:30:20Z", "ready-check-timer-after-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "active");
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("keeps the initial active state when the optional live state is missing", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:30:20Z", "ready-check-timer-missing-state-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "active");
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("expires locally at ends_at without a request", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:59:00Z", "ready-check-timer-end-boundary-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "active");
  await expect(readyButton(page)).toBeEnabled();
  await page.clock.runFor(59_000);
  await expect(readyButton(page)).toBeEnabled();
  await page.clock.runFor(1_000);
  await expect(page.getByRole("main").getByRole("button", { name: "Подтверждение закрыто" })).toBeDisabled();
  expect(requests).toHaveLength(0);
});

test("loads after ends_at as expired without a request", async ({ page }) => {
  await preparePage(page, "2026-06-07T16:00:00Z", "ready-check-timer-expired-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "finished");
  await expect(page.getByRole("main").getByRole("button", { name: "Подтверждение закрыто" })).toBeDisabled();
  expect(requests).toHaveLength(0);
});

test("recomputes on visibility restore without an HTTP probe", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-before-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "waiting");
  await expect(readyButton(page)).toBeDisabled();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(60_000);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("uses the server-relative monotonic timeline after a wall-clock change", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-before-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "waiting");
  await expect(readyButton(page)).toBeDisabled();
  await page.evaluate(() => {
    const originalDateNow = Date.now;
    Date.now = () => originalDateNow() + 24 * 60 * 60 * 1000;
  });
  await page.clock.runFor(60_000);
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("reloads with a fresh server timestamp before and after the window", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-before-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/tournaments/night-veil-open-5");
  await waitForReadyCheckTimer(page, "waiting");
  await expect(readyButton(page)).toBeDisabled();
  await page.context().clearCookies({ name: "ready-check-timer-before-smoke" });
  await page.context().addCookies([
    {
      name: "ready-check-timer-after-smoke",
      value: "1",
      url: "http://127.0.0.1:3100",
    },
  ]);
  await page.reload();
  await expect(readyButton(page)).toBeEnabled();
  expect(requests).toHaveLength(0);
});

test("does not create Ready Check traffic on unrelated pages", async ({ page }) => {
  await preparePage(page, "2026-06-07T15:29:00Z", "ready-check-timer-before-smoke");
  const requests = trackReadyCheckRequests(page);

  await page.goto("/profile/me");
  await page.goto("/");
  await page.goto("/tournaments/citadel-clash-3");
  expect(requests).toHaveLength(0);
});
