import { expect, test } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

const TOURNAMENT_A = "/tournaments/night-veil-open-5";

async function authenticateTestUser(page: Page) {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "registration-race-session",
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
}

function deferred(): {
  promise: Promise<void>;
  resolve: () => void;
} {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

async function navigateToTournamentB(page: Page) {
  await page
    .getByRole("navigation", { name: "Главная навигация" })
    .getByRole("link", { name: "Турниры", exact: true })
    .click();
  await expect(page).toHaveURL(/\/tournaments$/u);
  await page.context().clearCookies({ name: "teams-pending-smoke" });
  await page.getByRole("link", { name: "Открыть турнир: Citadel Clash #3" }).click();
  await expect(page).toHaveURL(/\/tournaments\/citadel-clash-3$/u);
  await expect(page.getByRole("heading", { level: 1, name: "Citadel Clash #3", exact: true })).toBeVisible();
}

test("a delayed A workspace response cannot replace the B tournament", async ({ page }) => {
  await authenticateTestUser(page);
  const aStarted = deferred();
  const releaseA = deferred();
  const aFinished = deferred();

  await page.route("**/api/v1/tournaments/night-veil-open-5/workspace*", async (route) => {
    aStarted.resolve();
    await releaseA.promise;
    try {
      await route.fallback();
    } catch {
      // The detail page aborts the old request when the route changes.
    } finally {
      aFinished.resolve();
    }
  });

  await page.goto(TOURNAMENT_A);
  await aStarted.promise;
  await navigateToTournamentB(page);

  releaseA.resolve();
  await aFinished.promise;
  await expect(page.getByRole("heading", { level: 1, name: "Citadel Clash #3", exact: true })).toBeVisible();
  await expect(page.getByTestId("registration-steps")).toContainText("Регистрация закрыта");
});

test("a delayed A registration response cannot alter B controls", async ({ page }) => {
  await authenticateTestUser(page);
  const aStarted = deferred();
  const releaseA = deferred();
  const aFinished = deferred();

  await page.route("**/api/v1/tournaments/night-veil-open-5/join", async (route: Route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    aStarted.resolve();
    await releaseA.promise;
    try {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "delayed-a-registration",
          user_id: "u_lisalexy",
          status: "registered",
          check_in_status: "pending",
          registered_at: "2026-06-07T15:35:00Z",
          checked_in_at: null
        })
      });
    } catch {
      // The action aborts the old request when the tournament changes.
    } finally {
      aFinished.resolve();
    }
  });

  await page.goto(TOURNAMENT_A);
  const steps = page.getByTestId("registration-steps");
  await expect(steps.getByRole("button", { name: "Зарегистрироваться" })).toBeEnabled();
  await steps.getByRole("button", { name: "Зарегистрироваться" }).click();
  await aStarted.promise;
  const pendingButton = steps.getByRole("button").first();
  await expect(pendingButton).toBeDisabled();
  await expect(pendingButton).toHaveAttribute("aria-busy", "true");

  await navigateToTournamentB(page);
  releaseA.resolve();
  await aFinished.promise;

  const bSteps = page.getByTestId("registration-steps");
  await expect(page.getByRole("heading", { level: 1, name: "Citadel Clash #3", exact: true })).toBeVisible();
  await expect(bSteps).toContainText("Регистрация закрыта");
  await expect(bSteps.getByRole("button", { name: "Отменить регистрацию" })).toHaveCount(0);
});
