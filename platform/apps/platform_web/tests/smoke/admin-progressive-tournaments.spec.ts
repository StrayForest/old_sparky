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
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).click();
  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(25);
  await expect(page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).locator("small")).toHaveText("27");
  await expect(page.getByText("Показано 25 из 27", { exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Tournament 01");
  await expect.poll(() => requestedOffsets).toEqual([0]);

  const loadMore = page.getByTestId("admin-tournaments-load-more");
  await loadMore.click();
  await expect(loadMore).toBeDisabled();
  await expect(page.getByTestId("admin-tournaments-page-error")).toBeVisible();
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(25);
  await expect.poll(() => requestedOffsets).toEqual([0, 25]);

  await page.getByTestId("admin-tournaments-page-retry").click();

  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(totalTournaments);
  await expect(page.getByTestId("admin-tournament-tournament-25")).toHaveCount(1);
  await expect(page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).locator("small")).toHaveText("27");
  await expect(page.getByText("Показано 27 из 27", { exact: true })).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Tournament 01");
  await expect(page.getByTestId("admin-tournaments-load-more")).toHaveCount(0);
  await expect(page.getByTestId("admin-tournaments-page-error")).toHaveCount(0);
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25]);

  await page.getByTestId("admin-tournament-tournament-27").click();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Tournament 27");
  await page.getByTestId("admin-refresh").click();

  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(25);
  await expect(page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).locator("small")).toHaveText("27");
  await expect(page.getByTestId("admin-tournaments-load-more")).toBeVisible();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Tournament 01");
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25, 0]);

  await page.getByTestId("admin-tournament-tournament-05").click();
  await page.getByTestId("admin-refresh").click();

  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(25);
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Tournament 05");
  await expect.poll(() => requestedOffsets).toEqual([0, 25, 25, 0, 0]);

  await page.getByTestId("admin-tournament-search").fill("Tournament 27");
  await expect(page.locator(".ops-tournament-table tbody tr")).toHaveCount(1);
  await expect(page.getByTestId("admin-tournament-tournament-27")).toBeVisible();
  await expect(page.getByText("Показано 1 из 1", { exact: true })).toBeVisible();
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

test("admin recovery switches drafts with the selected tournament and applies B defaults", async ({ page }) => {
  const tournamentA = adminTournamentFixture({
    id: "admin-selection-a",
    slug: "admin-selection-a",
    name: "Selection A",
    status: "in_progress",
    visibility: "public",
    registration_closes_at: "2026-07-01T12:00:00Z",
    ready_check_starts_at: "2026-07-01T12:30:00Z",
    ready_check_ends_at: "2026-07-01T13:00:00Z",
    captain_selection_starts_at: "2026-07-01T13:30:00Z",
    starts_at: "2026-07-01T14:00:00Z"
  });
  const tournamentB = adminTournamentFixture({
    id: "admin-selection-b",
    slug: "admin-selection-b",
    name: "Selection B",
    status: "registration_open",
    visibility: "public",
    registration_closes_at: "2026-08-02T12:00:00Z",
    ready_check_starts_at: "2026-08-02T12:30:00Z",
    ready_check_ends_at: "2026-08-02T13:00:00Z",
    captain_selection_starts_at: "2026-08-02T13:30:00Z",
    starts_at: "2026-08-02T14:00:00Z"
  });
  const mutationBodies: Array<{ slug: string; body: Record<string, unknown> }> = [];

  await setupAdminConsole(page, [tournamentA, tournamentB], async (route, path) => {
    if (route.request().method() !== "PATCH") return false;
    const slug = path.split("/").at(-1) ?? "";
    const body = route.request().postDataJSON() as Record<string, unknown>;
    mutationBodies.push({ slug, body });
    const source = slug === tournamentB.slug ? tournamentB : tournamentA;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...source,
        status: typeof body.status === "string" ? body.status : source.status,
        visibility: typeof body.visibility === "string" ? body.visibility : source.visibility
      })
    });
    return true;
  });

  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).click();
  await expect(page.getByTestId("admin-tournament-admin-selection-a")).toBeVisible();
  await page.getByRole("button", { name: "Recovery", exact: true }).click();

  await page.getByTestId("admin-status-override").selectOption("completed");
  await page.getByTestId("admin-override-note").fill("Repair A state before switching.");
  await page.getByTestId("admin-tournament-admin-selection-b").click();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Selection B");
  await expect(page.getByTestId("admin-status-override")).toHaveValue("registration_open");
  await expect(page.getByTestId("admin-visibility-override")).toHaveValue("public");
  await expect(page.getByTestId("admin-override-note")).toHaveValue("");
  await expect(page.getByTestId("admin-registration-closes-at")).toHaveValue(localDateTime(tournamentB.registration_closes_at));

  await page.getByTestId("admin-visibility-override").selectOption("invite_only");
  await page.getByTestId("admin-override-note").fill("Repair B state after switching.");
  await page.getByTestId("admin-apply-override").click();
  await expect(page.getByText("Изменение сохранено и записано в audit.")).toBeVisible();
  expect(mutationBodies).toHaveLength(1);
  expect(mutationBodies[0]).toMatchObject({
    slug: tournamentB.slug,
    body: {
      status: null,
      visibility: "invite_only",
      note: "Repair B state after switching.",
      registration_closes_at: normalizedIso(tournamentB.registration_closes_at),
      ready_check_starts_at: normalizedIso(tournamentB.ready_check_starts_at),
      ready_check_ends_at: normalizedIso(tournamentB.ready_check_ends_at),
      captain_selection_starts_at: normalizedIso(tournamentB.captain_selection_starts_at),
      starts_at: normalizedIso(tournamentB.starts_at)
    }
  });
});

test("admin recovery ignores a delayed A mutation after switching to B", async ({ page }) => {
  const tournamentA = adminTournamentFixture({ id: "admin-delayed-a", slug: "admin-delayed-a", name: "Delayed A", status: "in_progress" });
  const tournamentB = adminTournamentFixture({ id: "admin-delayed-b", slug: "admin-delayed-b", name: "Delayed B", status: "registration_open" });
  let releaseA!: () => void;
  let resolveAStarted!: () => void;
  let resolveAFinished!: () => void;
  let aResponseReleased = false;
  const aStarted = new Promise<void>((resolve) => { resolveAStarted = resolve; });
  const aFinished = new Promise<void>((resolve) => { resolveAFinished = resolve; });
  const aResponse = new Promise<void>((resolve) => { releaseA = () => { aResponseReleased = true; resolve(); }; });

  await setupAdminConsole(page, [tournamentA, tournamentB], async (route, path) => {
    if (route.request().method() !== "PATCH" || !path.endsWith(`/${tournamentA.slug}`)) return false;
    resolveAStarted();
    await aResponse;
    try {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...tournamentA, status: "completed" })
      });
    } catch {
      // The component aborts the request when its tournament identity unmounts.
    } finally {
      resolveAFinished();
    }
    return true;
  });

  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).click();
  await page.getByRole("button", { name: "Recovery", exact: true }).click();
  await page.getByTestId("admin-status-override").selectOption("completed");
  await page.getByTestId("admin-override-note").fill("Delayed A mutation.");
  await page.getByTestId("admin-apply-override").click();
  await aStarted;

  await page.getByTestId("admin-tournament-admin-delayed-b").click();
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Delayed B");
  await expect(page.getByTestId("admin-status-override")).toHaveValue("registration_open");
  await expect(page.getByTestId("admin-override-note")).toHaveValue("");
  await expect(page.getByTestId("admin-apply-override")).toBeDisabled();

  releaseA();
  await aFinished;
  expect(aResponseReleased).toBe(true);
  await expect(page.getByTestId("admin-tournament-inspector").getByRole("heading", { level: 2 })).toHaveText("Delayed B");
  await expect(page.getByTestId("admin-status-override")).toHaveValue("registration_open");
  await expect(page.getByText("Изменение сохранено и записано в audit.")).toHaveCount(0);
});

test("admin roster resets local controls and permissions on selection switch", async ({ page }) => {
  const tournamentA = adminTournamentFixture({ id: "admin-roster-a", slug: "admin-roster-a", name: "Roster A", status: "in_progress" });
  const tournamentB = adminTournamentFixture({ id: "admin-roster-b", slug: "admin-roster-b", name: "Roster B", status: "in_progress" });
  const rosterA = adminRosterFixture(tournamentA, { requires_override: true, can_override: true });
  const rosterB = adminRosterFixture(tournamentB, {
    can_add_player: false,
    can_remove_player: false,
    can_move_player: false,
    can_replace_player: false,
    can_change_captain: false,
    requires_override: false,
    can_override: false
  });
  let releaseA!: () => void;
  let resolveAStarted!: () => void;
  let resolveAFinished!: () => void;
  const aStarted = new Promise<void>((resolve) => { resolveAStarted = resolve; });
  const aFinished = new Promise<void>((resolve) => { resolveAFinished = resolve; });
  const aResponse = new Promise<void>((resolve) => { releaseA = resolve; });

  await setupAdminConsole(page, [tournamentA, tournamentB], async (route, path) => {
    const method = route.request().method();
    const rosterMatch = path.match(/\/admin\/tournaments\/([^/]+)\/roster(?:\/[^/]+)?$/u);
    if (!rosterMatch) return false;
    const slug = rosterMatch[1];
    if (method === "GET") {
      const roster = slug === tournamentB.slug ? rosterB : rosterA;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(roster) });
      return true;
    }
    if (method !== "POST" || slug !== tournamentA.slug) return false;
    resolveAStarted();
    await aResponse;
    try {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...rosterA, state_version: 2 }) });
    } catch {
      // The component aborts the request after the roster identity changes.
    } finally {
      resolveAFinished();
    }
    return true;
  });

  await page.goto("/platform-ops");
  await expect(page.getByTestId("admin-console")).toBeVisible();
  await page.getByLabel("Навигация админ-панели").getByRole("button", { name: /Турниры/ }).click();
  await expect(page.getByTestId("admin-tournament-admin-roster-a")).toBeVisible();
  await page.getByRole("button", { name: "Ростер", exact: true }).click();
  await expect(page.getByTestId("admin-roster-panel")).toBeVisible();
  await page.getByRole("button", { name: /Roster A Player/ }).click();
  await page.getByTestId("admin-roster-panel").getByRole("checkbox").check();
  await page.getByTestId("admin-roster-panel").getByRole("textbox").fill("Replace A while switching.");
  const addButton = page.getByTestId("admin-roster-panel").getByRole("button", { name: "Добавить", exact: true });
  await expect(addButton).toBeEnabled();
  await addButton.click();
  await aStarted;

  await page.getByTestId("admin-tournament-admin-roster-b").click();
  await expect(page.getByTestId("admin-roster-panel")).toBeVisible();
  await expect(page.getByText("Roster B Player")).toBeVisible();
  await expect(page.getByTestId("admin-roster-panel").getByRole("textbox")).toHaveValue("");
  await expect(page.getByTestId("admin-roster-panel").getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByTestId("admin-roster-panel").getByRole("button", { name: "Добавить", exact: true })).toBeDisabled();

  releaseA();
  await aFinished;
  await expect(page.getByText("Roster B Player")).toBeVisible();
  await expect(page.getByTestId("admin-roster-panel").getByRole("button", { name: "Добавить", exact: true })).toBeDisabled();
});

type AdminTournamentFixture = Record<string, unknown> & { id: string; slug: string; name: string; status: string; visibility: string };

function adminTournamentFixture(overrides: Partial<AdminTournamentFixture> = {}): AdminTournamentFixture {
  return {
    id: "admin-selection-default",
    slug: "admin-selection-default",
    name: "Admin Selection Default",
    description: "Deterministic admin selection fixture",
    visibility: "public",
    status: "in_progress",
    format_slug: "solo",
    organizer_user_id: "u_organizer",
    organizer_display_name: "Tournament Owner",
    participant_count: 8,
    max_participants: 32,
    allowed_ranks: ["r1"],
    has_locked_deadlock_roster: false,
    registration_starts_at: "2026-06-01T12:00:00Z",
    registration_closes_at: "2026-06-01T13:00:00Z",
    ready_check_starts_at: "2026-06-01T13:30:00Z",
    ready_check_ends_at: "2026-06-01T14:00:00Z",
    captain_selection_starts_at: "2026-06-01T14:30:00Z",
    starts_at: "2026-06-01T15:00:00Z",
    created_at: "2026-06-01T12:00:00Z",
    available_next_statuses: ["registration_closed", "cancelled"],
    match_count: 0,
    latest_round_number: null,
    unfinished_match_count: 0,
    completed_match_count: 0,
    cancelled_match_count: 0,
    admin_override_warning: null,
    admin_recovery_hint: null,
    ...overrides
  };
}

function adminRosterFixture(tournament: AdminTournamentFixture, capabilities: Partial<PlatformAdminRosterFixture["capabilities"]> = {}): PlatformAdminRosterFixture {
  return {
    tournament_id: tournament.id,
    tournament_slug: tournament.slug,
    tournament_status: tournament.status,
    active_participant_count: 1,
    state_version: 1,
    source_assignment_run_id: null,
    source_assignment_status: null,
    locked: false,
    manually_modified: false,
    last_modified_at: null,
    bracket: { exists: false, revision: 0, match_count: 0, started_count: 0, completed_count: 0 },
    teams: [{
      id: `${tournament.slug}-team`,
      team_key: "team-a",
      name: `${tournament.name} Team`,
      captain_user_id: `${tournament.slug}-captain`,
      starter_strength: 100,
      starter_average_strength: 100,
      members: [{
        id: `${tournament.slug}-member`,
        user_id: `${tournament.slug}-captain`,
        display_name: `${tournament.name} Player`,
        handle: null,
        participant_status: "registered",
        slot_number: 1,
        roster_role: "starter",
        assigned_role: "Carry",
        strength: 100,
        rank: "Initiate",
        subrank: 1
      }]
    }],
    unassigned_participants: [{
      participant_id: `${tournament.slug}-unassigned-participant`,
      user_id: `${tournament.slug}-unassigned`,
      display_name: `${tournament.name} Unassigned`,
      handle: null,
      status: "registered",
      rank: "Initiate",
      subrank: 1,
      playtime: "100",
      strength: 80
    }],
    capabilities: {
      can_add_player: true,
      can_remove_player: true,
      can_move_player: true,
      can_replace_player: true,
      can_change_captain: true,
      requires_override: false,
      can_override: false,
      blocked_reason: null,
      ...capabilities
    }
  };
}

type PlatformAdminRosterFixture = {
  tournament_id: string;
  tournament_slug: string;
  tournament_status: string;
  active_participant_count: number;
  state_version: number;
  source_assignment_run_id: string | null;
  source_assignment_status: string | null;
  locked: boolean;
  manually_modified: boolean;
  last_modified_at: string | null;
  bracket: { exists: boolean; revision: number; match_count: number; started_count: number; completed_count: number };
  teams: Array<Record<string, unknown>>;
  unassigned_participants: Array<Record<string, unknown>>;
  capabilities: {
    can_add_player: boolean;
    can_remove_player: boolean;
    can_move_player: boolean;
    can_replace_player: boolean;
    can_change_captain: boolean;
    requires_override: boolean;
    can_override: boolean;
    blocked_reason: string | null;
  };
};

async function setupAdminConsole(
  page: import("@playwright/test").Page,
  tournaments: AdminTournamentFixture[],
  onTournamentRequest?: (route: import("@playwright/test").Route, path: string) => Promise<boolean>
) {
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "admin-selection-session",
    url: "http://127.0.0.1:3100"
  }]);
  await page.route("**/api/v1/admin/overview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ users_total: 2, tournaments_total: tournaments.length, tournaments_attention_total: 0, audit_events_total: 0 })
    });
  });
  await page.route("**/api/v1/admin/users**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/admin/audit-logs**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/admin/preprod-test-runs**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/admin/tournaments**", async (route) => {
    const requestPath = new URL(route.request().url()).pathname;
    if (onTournamentRequest && await onTournamentRequest(route, requestPath)) return;
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "X-Total-Count": String(tournaments.length), "X-Limit": "25", "X-Offset": "0", "X-Has-More": "false" },
        body: JSON.stringify(tournaments)
      });
      return;
    }
    await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Unhandled admin tournament mutation." }) });
  });
}

function localDateTime(value: unknown): string {
  const date = new Date(String(value));
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 16);
}

function normalizedIso(value: unknown): string {
  return new Date(String(value)).toISOString();
}
