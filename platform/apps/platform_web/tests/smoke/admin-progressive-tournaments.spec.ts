import { expect, test } from "@playwright/test";

const totalTournaments = 27;

function tournament(index: number) {
  const paddedIndex = String(index).padStart(2, "0");
  return {
    id: `tournament-${paddedIndex}`,
    slug: `tournament-${paddedIndex}`,
    name: `Tournament ${paddedIndex}`,
    description: `Progressive admin tournament ${paddedIndex}`,
    visibility: "public",
    status: "registration_open",
    format_slug: "solo",
    organizer_user_id: "u_organizer",
    organizer_display_name: "Tournament Owner",
    participant_count: index,
    max_participants: 32,
    allowed_ranks: ["r1"],
    has_locked_deadlock_roster: false,
    created_at: "2026-06-01T12:00:00Z",
    available_next_statuses: ["registration_closed", "cancelled"],
    match_count: 0,
    latest_round_number: null,
    unfinished_match_count: 0,
    completed_match_count: 0,
    cancelled_match_count: 0,
    admin_override_warning: null,
    admin_recovery_hint: null
  };
}

test("admin tournament list progressively loads, retries, and deduplicates pages", async ({ page }) => {
  const firstPage = Array.from({ length: 25 }, (_, index) => tournament(index + 1));
  const filteredTournament = {
    ...tournament(27),
    visibility: "invite_only",
    status: "completed",
    unfinished_match_count: 1,
    admin_override_warning: "Review this tournament."
  };
  const secondPage = [firstPage[24], tournament(26), filteredTournament];
  const requestedOffsets: number[] = [];
  const requestedQueries: URLSearchParams[] = [];
  let secondPageAttempts = 0;

  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-smoke-session",
    url: "http://127.0.0.1:3100"
  }]);

  await page.route("**/api/v1/admin/overview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        users_total: 1,
        tournaments_total: totalTournaments,
        tournaments_attention_total: 1,
        audit_events_total: 0
      })
    });
  });

  await page.route("**/api/v1/admin/users", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });

  await page.route("**/api/v1/admin/audit-logs?*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });

  await page.route("**/api/v1/admin/preprod-test-runs**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });

  await page.route("**/api/v1/admin/tournaments?*", async (route) => {
    const requestUrl = new URL(route.request().url());
    const limit = Number(requestUrl.searchParams.get("limit"));
    const offset = Number(requestUrl.searchParams.get("offset"));
    requestedQueries.push(requestUrl.searchParams);
    requestedOffsets.push(offset);
    expect(limit).toBe(25);

    if (
      requestUrl.searchParams.has("search")
      || requestUrl.searchParams.has("status")
      || requestUrl.searchParams.has("visibility")
      || requestUrl.searchParams.has("attention")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: {
          "X-Total-Count": "1",
          "X-Limit": "25",
          "X-Offset": "0",
          "X-Has-More": "false"
        },
        body: JSON.stringify([filteredTournament])
      });
      return;
    }

    if (offset === 0) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: {
          "X-Total-Count": String(totalTournaments),
          "X-Limit": "25",
          "X-Offset": "0",
          "X-Has-More": "true"
        },
        body: JSON.stringify(firstPage)
      });
      return;
    }

    expect(offset).toBe(25);
    secondPageAttempts += 1;
    if (secondPageAttempts === 1) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Temporary tournament page failure." })
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "X-Total-Count": String(totalTournaments),
        "X-Limit": "25",
        "X-Offset": "25",
        "X-Has-More": "false"
      },
      body: JSON.stringify(secondPage)
    });
  });

  await page.goto("/platform-ops");

  await expect(page.getByTestId("admin-console")).toBeVisible();
  await expect(page.locator(".admin-table tbody tr")).toHaveCount(25);
  await expect(page.getByRole("tab").first().locator("span")).toHaveText("25/27");
  await expect(page.getByText("Записей: 27", { exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading")).toHaveText("Tournament 01");
  await expect.poll(() => requestedOffsets).toEqual([0]);

  const loadMore = page.getByTestId("admin-tournaments-load-more");
  await loadMore.click();
  await expect(loadMore).toBeDisabled();
  await expect(page.getByTestId("admin-tournaments-page-error")).toBeVisible();
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await expect(page.locator(".admin-table tbody tr")).toHaveCount(25);
  await expect.poll(() => requestedOffsets).toEqual([0, 25]);

  await page.getByTestId("admin-tournaments-page-retry").click();

  await expect(page.locator(".admin-table tbody tr")).toHaveCount(totalTournaments);
  await expect(page.getByTestId("admin-tournament-tournament-25")).toHaveCount(1);
  await expect(page.getByRole("tab").first().locator("span")).toHaveText("27/27");
  await expect(page.getByText("Записей: 27", { exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading")).toHaveText("Tournament 01");
  await expect(page.getByTestId("admin-tournaments-load-more")).toHaveCount(0);
  await expect(page.getByTestId("admin-tournaments-page-error")).toHaveCount(0);
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25]);

  await page.getByTestId("admin-tournament-tournament-27").click();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading")).toHaveText("Tournament 27");
  await page.getByTestId("admin-refresh").click();

  await expect(page.locator(".admin-table tbody tr")).toHaveCount(25);
  await expect(page.getByRole("tab").first().locator("span")).toHaveText("25/27");
  await expect(page.getByTestId("admin-tournaments-load-more")).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading")).toHaveText("Tournament 01");
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25, 0]);

  await page.getByTestId("admin-tournament-tournament-05").click();
  await page.getByTestId("admin-refresh").click();

  await expect(page.locator(".admin-table tbody tr")).toHaveCount(25);
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading")).toHaveText("Tournament 05");
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25, 0, 0]);

  await page.getByTestId("admin-tournament-search").fill("Tournament 27");
  await expect(page.locator(".admin-table tbody tr")).toHaveCount(1);
  await expect(page.getByTestId("admin-tournament-tournament-27")).toBeVisible();
  await expect(page.getByText("Записей: 1", { exact: true })).toBeVisible();
  await page.getByTestId("admin-tournament-status-filter").selectOption("completed");
  await page.getByTestId("admin-tournament-visibility-filter").selectOption("invite_only");
  await page.getByTestId("admin-tournament-attention-filter").click();
  await expect(page.getByTestId("admin-tournament-tournament-27")).toBeVisible();
  await expect.poll(() => requestedQueries.at(-1)?.get("attention")).toBe("true");
  const finalQuery = requestedQueries.at(-1);
  expect(finalQuery?.get("search")).toBe("Tournament 27");
  expect(finalQuery?.get("status")).toBe("completed");
  expect(finalQuery?.get("visibility")).toBe("invite_only");
  expect(finalQuery?.get("attention")).toBe("true");
  await expectNoHorizontalOverflow(page);
});

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  const overflow = await page.evaluate(() => (
    Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
  ));
  expect(overflow).toBeLessThanOrEqual(2);
}
