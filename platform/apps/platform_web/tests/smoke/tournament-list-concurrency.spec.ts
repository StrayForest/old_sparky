import { expect, test } from "@playwright/test";

function tournament(id: string, name: string, status: string) {
  return {
    id,
    slug: id,
    name,
    description: "Concurrency regression tournament",
    visibility: "public",
    status,
    format_slug: "solo",
    organizer_user_id: "u_test",
    organizer_display_name: "Test Organizer",
    participant_count: 0,
    max_participants: 16,
    teams_count: 8,
    allowed_ranks: ["Initiate"],
    starts_at: "2026-09-01T17:00:00Z"
  };
}

function pagedHeaders(limit = 9) {
  return {
    "X-Limit": String(limit),
    "X-Has-More": "false"
  };
}

test("tournament list reuses the server-rendered first page without a duplicate browser fetch", async ({ page }) => {
  const browserTournamentRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/tournaments") {
      browserTournamentRequests.push(url.search);
    }
  });

  await page.goto("/tournaments");
  await expect(page.getByTestId("tournament-card").first()).toBeVisible();

  expect(browserTournamentRequests).toEqual([]);
});

test("tournament filters keep the newest response when an older request finishes later", async ({ page }) => {
  let releaseOpenRequest = () => {};
  const openRelease = new Promise<void>((resolve) => {
    releaseOpenRequest = resolve;
  });
  let markOpenStarted = () => {};
  const openStarted = new Promise<void>((resolve) => {
    markOpenStarted = resolve;
  });

  await page.route("**/api/v1/tournaments?**", async (route) => {
    const url = new URL(route.request().url());
    const status = url.searchParams.get("status");

    if (status === "registration_open") {
      markOpenStarted();
      await openRelease;
      try {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: pagedHeaders(),
          body: JSON.stringify([
            tournament("stale-open", "Stale Open Cup", "registration_open")
          ])
        });
      } catch {
        // Expected when the older browser request is aborted by the newer filter.
      }
      return;
    }

    if (status === "completed") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: pagedHeaders(),
        body: JSON.stringify([
          tournament("fresh-completed", "Fresh Completed Cup", "completed")
        ])
      });
      return;
    }

    await route.fallback();
  });

  await page.goto("/tournaments");
  await page.getByTestId("status-filter").selectOption("registration_open");
  await openStarted;

  await page.getByTestId("status-filter").selectOption("completed");
  await expect(page.getByRole("heading", { name: "Fresh Completed Cup" })).toBeVisible();

  releaseOpenRequest();

  await expect(page.getByRole("heading", { name: "Fresh Completed Cup" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Stale Open Cup" })).toHaveCount(0);
});
