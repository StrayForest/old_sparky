import { createServer, type Server, type ServerResponse } from "node:http";
import { expect, test } from "@playwright/test";
import type { PlatformTournament } from "../../lib/platform-types";

const apiHost = "127.0.0.1";
const apiPort = 3199;
const webBaseUrl = "http://127.0.0.1:3101";
const publicTournamentSlug = "lean-detail-cup";
const readyTournamentSlug = "lean-ready-cup";
const privateBearerSlug = "private-bearer-cup";
const privateBearerCode = "VALIDBEARER26";
const expiredBearerCode = "EXPIREDBEARER26";
const revokedBearerCode = "REVOKEDBEARER26";
const wrongBearerCode = "WRONGBEARER26";
const privateBearerInactiveCookie = "private-bearer-inactive=1";
const actorUserId = "user-actor";
const apiRequests: string[] = [];
const workspaceRequests: Array<{
  slug: string;
  participantsLimit: number;
  participantsOffset: number;
  workspaceView: string;
  includeCurrentUser: boolean;
}> = [];
const participantRequests: string[] = [];
const bearerWorkspaceRequests: Array<{
  slug: string;
  inviteCode: string | null;
  workspaceView: string;
}> = [];
const bearerResponseStatuses: Array<{ code: string | null; status: number }> = [];
let usersMeRequests = 0;
let authBootstrapRequests = 0;
let csrfRequests = 0;
let readyVoteRequests = 0;
let forcedReadyVoteOverloads = 0;
let holdFirstReadyVoteResponse = false;
let releaseHeldReadyVoteResponse: (() => void) | null = null;
let bracketRequests = 0;
let inviteClaimRequests = 0;

let apiServer: Server | null = null;

test.setTimeout(60_000);

test.beforeAll(async () => {
  apiServer = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", `http://${apiHost}:${apiPort}`);
    apiRequests.push(sanitizedRequestLog(request.method ?? "GET", url));
    const hasTestCookie = request.headers.cookie?.includes("lean-detail-smoke=1") ?? false;

    if (url.pathname === "/api/v1/auth/bootstrap") {
      authBootstrapRequests += 1;
      respondJson(response, hasTestCookie ? 200 : 404, hasTestCookie ? platformAuthBootstrap() : {
        detail: "Not found."
      });
      return;
    }

    if (url.pathname === "/api/v1/users/me") {
      usersMeRequests += 1;
      respondJson(response, hasTestCookie ? 200 : 404, hasTestCookie ? platformUser() : {
        detail: "Not found."
      });
      return;
    }

    if (url.pathname === "/api/v1/auth/csrf") {
      csrfRequests += 1;
      respondJson(response, 200, { csrf_token: `lean.${"c".repeat(64)}` });
      return;
    }

    if (url.pathname === "/api/v1/tournaments/invites/claim") {
      inviteClaimRequests += 1;
      respondJson(response, 404, { detail: "Not found." });
      return;
    }

    const workspaceMatch = url.pathname.match(/^\/api\/v1\/tournaments\/([^/]+)\/workspace$/);
    if (workspaceMatch) {
      const slug = workspaceMatch[1] ?? publicTournamentSlug;
      const inviteCode = url.searchParams.get("invite_code");
      workspaceRequests.push({
        slug,
        participantsLimit: Number(url.searchParams.get("participants_limit") ?? 25),
        participantsOffset: Number(url.searchParams.get("participants_offset") ?? 0),
        workspaceView: url.searchParams.get("workspace_view") ?? "bracket",
        includeCurrentUser: url.searchParams.get("include_current_user") !== "false"
      });
      if (slug === privateBearerSlug) {
        bearerWorkspaceRequests.push({
          slug,
          inviteCode,
          workspaceView: url.searchParams.get("workspace_view") ?? "bracket"
        });
        const bearerStatus = privateBearerStatus(inviteCode);
        if (bearerStatus !== null) {
          bearerResponseStatuses.push({ code: inviteCode, status: bearerStatus });
          respondJson(response, bearerStatus, { detail: "Private tournament access denied." });
          return;
        }
        bearerResponseStatuses.push({ code: inviteCode, status: 200 });
      }
      respondJson(response, 200, workspacePayload(
        slug,
        hasTestCookie,
        url.searchParams.get("include_current_user") !== "false",
        inviteCode,
        request.headers.cookie?.includes(privateBearerInactiveCookie) ?? false
      ));
      return;
    }

    if (url.pathname.match(/^\/api\/v1\/tournaments\/[^/]+\/bracket$/)) {
      bracketRequests += 1;
      respondJson(response, 200, {
        tournament_id: `t_${publicTournamentSlug}`,
        status: "pending",
        revision: 0,
        can_manage: false,
        teams: [],
        matches: []
      });
      return;
    }

    if (url.pathname.match(/^\/api\/v1\/tournaments\/[^/]+\/participants$/)) {
      participantRequests.push(sanitizedRequestLog(request.method ?? "GET", url));
      respondJson(response, 200, [], {
        "Access-Control-Expose-Headers": "X-Total-Count, X-Limit, X-Offset, X-Has-More",
        "X-Total-Count": "0",
        "X-Limit": url.searchParams.get("limit") ?? "25",
        "X-Offset": url.searchParams.get("offset") ?? "0",
        "X-Has-More": "false"
      });
      return;
    }

    if (
      url.pathname === `/api/v1/tournaments/${readyTournamentSlug}/deadlock/ready-check/vote`
      && request.method === "POST"
    ) {
      readyVoteRequests += 1;
      if (forcedReadyVoteOverloads > 0) {
        forcedReadyVoteOverloads -= 1;
        if (holdFirstReadyVoteResponse) {
          await new Promise<void>((resolve) => {
            releaseHeldReadyVoteResponse = resolve;
          });
        }
        respondJson(response, 503, {
          code: "READY_VOTE_OVERLOADED",
          retryable: true,
          retry_after_ms: 150
        }, { "Retry-After": "1" });
        return;
      }
      respondJson(response, 200, readyVoteResponse("yes"));
      return;
    }

    respondJson(response, 404, { detail: "Not found." });
  });

  await new Promise<void>((resolve, reject) => {
    apiServer?.once("error", reject);
    apiServer?.listen(apiPort, apiHost, resolve);
  });
});

test.beforeEach(() => {
  apiRequests.length = 0;
  workspaceRequests.length = 0;
  participantRequests.length = 0;
  bearerWorkspaceRequests.length = 0;
  bearerResponseStatuses.length = 0;
  usersMeRequests = 0;
  authBootstrapRequests = 0;
  csrfRequests = 0;
  readyVoteRequests = 0;
  forcedReadyVoteOverloads = 0;
  holdFirstReadyVoteResponse = false;
  releaseHeldReadyVoteResponse = null;
  bracketRequests = 0;
  inviteClaimRequests = 0;
});

test.afterEach(() => {
  holdFirstReadyVoteResponse = false;
  releaseHeldReadyVoteResponse?.();
  releaseHeldReadyVoteResponse = null;
});

test.afterAll(async () => {
  await new Promise<void>((resolve, reject) => {
    if (!apiServer) {
      resolve();
      return;
    }
    apiServer.close((error) => error ? reject(error) : resolve());
  });
  apiServer = null;
});

test("tournament detail skips participant roster payload and session refetch", async ({ page }) => {
  const documentResponse = await page.goto(`/tournaments/${publicTournamentSlug}`);
  const serverHtml = await documentResponse?.text() ?? "";
  expect(
    documentResponse?.status(),
    `API requests: ${JSON.stringify(apiRequests)}; body: ${serverHtml.slice(0, 500)}`
  ).toBe(200);

  expect(serverHtml).not.toContain("data-testid=\"tournament-participant-roster\"");
  expect(serverHtml).not.toContain("SSR Player");
  await expectWorkspaceRequest({
    slug: publicTournamentSlug,
    participantsLimit: 0,
    participantsOffset: 0,
    workspaceView: "detail",
    includeCurrentUser: false
  });
  await expect(page.getByTestId("tournament-participant-roster")).toHaveCount(0);
  await expect(page.locator(".participants-value").first()).toHaveText("26 / 64");
  await expect.poll(() => participantRequests).toEqual([]);
  await expect.poll(() => usersMeRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("registered detail uses compact workspace state and ready vote avoids full refresh", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-07-20T16:10:00Z") });
  await blockNextRoutePrefetch(page);
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "lean-detail-session",
    url: webBaseUrl
  }, {
    name: "lean-detail-smoke",
    value: "1",
    url: webBaseUrl
  }]);

  const response = await page.goto(`/tournaments/${readyTournamentSlug}`);
  expect(response?.status()).toBe(200);
  await expectWorkspaceRequest({
    slug: readyTournamentSlug,
    participantsLimit: 0,
    participantsOffset: 0,
    workspaceView: "detail",
    includeCurrentUser: false
  });
  await expect(page.getByRole("button", { name: "Отменить регистрацию" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Подтвердить участие" })).toBeEnabled();
  await expect.poll(() => authBootstrapRequests).toBe(1);

  const workspaceRequestCountBeforeVote = workspaceRequests.length;
  await page.getByRole("button", { name: "Подтвердить участие" }).click();
  await expect(page.getByRole("button", { name: "Отменить подтверждение" })).toBeVisible();
  await expect(page.getByText("Участие подтверждено").last()).toBeVisible();
  await expect.poll(() => readyVoteRequests).toBe(1);
  expect(workspaceRequests).toHaveLength(workspaceRequestCountBeforeVote);
  await expect.poll(() => participantRequests).toEqual([]);
  await expect.poll(() => usersMeRequests).toBe(0);
  await expect.poll(() => csrfRequests).toBe(1);
  await expectNoHorizontalOverflow(page);
});

test("ready vote retries bounded overloads as one logical action", async ({ page }) => {
  forcedReadyVoteOverloads = 2;
  holdFirstReadyVoteResponse = true;
  await page.clock.install({ time: new Date("2026-07-20T16:10:00Z") });
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "lean-detail-session",
    url: webBaseUrl
  }, {
    name: "lean-detail-smoke",
    value: "1",
    url: webBaseUrl
  }]);

  await page.goto(`/tournaments/${readyTournamentSlug}`);
  const readyButton = page.getByTestId("ready-check-step").getByRole("button");
  await readyButton.click();
  await expect(readyButton).toHaveAttribute("aria-busy", "true");
  await readyButton.dispatchEvent("click");
  await expect.poll(() => readyVoteRequests).toBe(1);
  await expect.poll(() => releaseHeldReadyVoteResponse !== null).toBe(true);
  holdFirstReadyVoteResponse = false;
  releaseHeldReadyVoteResponse?.();
  releaseHeldReadyVoteResponse = null;
  await page.clock.fastForward(500);
  await expect.poll(() => readyVoteRequests).toBe(2);
  await page.clock.fastForward(1_000);
  await expect(page.getByRole("button", { name: "Отменить подтверждение" })).toBeVisible({ timeout: 5_000 });
  await expect.poll(() => readyVoteRequests).toBe(3);
  expect(forcedReadyVoteOverloads).toBe(0);
});

test("bracket page uses the initial workspace and has no background refresh", async ({ page }) => {
  await page.clock.install();
  await blockNextRoutePrefetch(page);
  await page.context().addCookies([{
    name: "deadlock_platform_session",
    value: "lean-bracket-session",
    url: webBaseUrl
  }, {
    name: "lean-detail-smoke",
    value: "1",
    url: webBaseUrl
  }]);
  const response = await page.goto(`/tournaments/${publicTournamentSlug}/bracket`);
  expect(response?.status()).toBe(200);
  expect(workspaceRequests).toEqual([{
    slug: publicTournamentSlug,
    participantsLimit: 0,
    participantsOffset: 0,
    workspaceView: "bracket",
    includeCurrentUser: false
  }]);
  await expect.poll(() => authBootstrapRequests).toBe(1);
  expect(bracketRequests).toBe(0);
  expect(participantRequests).toEqual([]);
  await expect.poll(() => csrfRequests).toBe(0);
  // Exercise the full no-refresh observation window through the browser clock;
  // an accidental polling interval would fire while this virtual time runs.
  await page.clock.runFor(4_000);
  expect(bracketRequests).toBe(0);
  await expectNoHorizontalOverflow(page);
});

test("anonymous bearer keeps the exact code across detail, bracket navigation, and reload", async ({ page }) => {
  await blockNextRoutePrefetch(page);
  const lowerCaseCode = privateBearerCode.toLowerCase();
  const detailPath = `/tournaments/${privateBearerSlug}?invite_code=${lowerCaseCode}`;
  const bracketPath = `/tournaments/${privateBearerSlug}/bracket?invite_code=${privateBearerCode}`;

  const detailResponse = await page.goto(detailPath);
  expect(detailResponse?.status()).toBe(200);
  await expect(page.getByTestId("tournament-read-only-registration")).toBeVisible();
  await expect(page.getByTestId("tournament-read-only-workflow")).toBeVisible();
  await expect(page.getByTestId("registration-steps").getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Зарегистрироваться" })).toHaveCount(0);
  await expect(page.locator("main a[href^='/profile']")).toHaveCount(0);

  const bracketLink = page.locator("a.bracket-open-link");
  await expect(bracketLink).toHaveAttribute("href", bracketPath);
  expect(bearerWorkspaceRequests).toEqual([{
    slug: privateBearerSlug,
    inviteCode: privateBearerCode,
    workspaceView: "detail"
  }, {
    slug: privateBearerSlug,
    inviteCode: privateBearerCode,
    workspaceView: "detail"
  }]);

  await bracketLink.click();
  await expect(page).toHaveURL(`${webBaseUrl}${bracketPath}`);
  await expect(page.getByTestId("bracket-match")).toHaveCount(1);
  await expect(page.locator("[data-testid='bracket-match'] input")).toHaveCount(0);
  await expect(page.locator("[data-testid='bracket-match'] button")).toHaveCount(0);
  await expect(page.locator("main a[href^='/profile']")).toHaveCount(0);
  expect(bearerWorkspaceRequests).toEqual([
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "detail"
    },
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "detail"
    },
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "bracket"
    }
  ]);

  await page.reload();
  await expect(page).toHaveURL(`${webBaseUrl}${bracketPath}`);
  await expect(page.getByTestId("bracket-match")).toHaveCount(1);
  expect(bearerWorkspaceRequests).toEqual([
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "detail"
    },
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "detail"
    },
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "bracket"
    },
    {
      slug: privateBearerSlug,
      inviteCode: privateBearerCode,
      workspaceView: "bracket"
    }
  ]);
  expect(inviteClaimRequests).toBe(0);
  expect(apiRequests.join("\n")).not.toContain(privateBearerCode);
  await expectNoHorizontalOverflow(page);
});

test("inactive retained bearer is visibly read-only without join, profile, or workflow controls", async ({ page }) => {
  await blockNextRoutePrefetch(page);
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "private-bearer-session",
      url: webBaseUrl
    },
    {
      name: "lean-detail-smoke",
      value: "1",
      url: webBaseUrl
    },
    {
      name: "private-bearer-inactive",
      value: "1",
      url: webBaseUrl
    }
  ]);

  const detailResponse = await page.goto(
    `/tournaments/${privateBearerSlug}?invite_code=${privateBearerCode}`
  );
  expect(detailResponse?.status()).toBe(200);
  await expect(page.getByTestId("tournament-read-only-registration")).toBeVisible();
  await expect(page.getByTestId("tournament-read-only-workflow")).toBeVisible();
  await expect(page.getByText("Доступ только для просмотра", { exact: true })).toBeVisible();
  await expect(page.getByTestId("registration-steps").getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Зарегистрироваться" })).toHaveCount(0);
  await expect(page.locator("main a[href^='/profile']")).toHaveCount(0);

  await page.locator("a.bracket-open-link").click();
  await expect(page).toHaveURL(`${webBaseUrl}/tournaments/${privateBearerSlug}/bracket?invite_code=${privateBearerCode}`);
  await expect(page.getByTestId("bracket-match")).toHaveCount(1);
  await expect(page.locator("[data-testid='bracket-match'] input")).toHaveCount(0);
  await expect(page.locator("[data-testid='bracket-match'] button")).toHaveCount(0);
  await expect(page.locator("main a[href^='/profile']")).toHaveCount(0);
  expect(inviteClaimRequests).toBe(0);
  expect(apiRequests.join("\n")).not.toContain(privateBearerCode);
  await expectNoHorizontalOverflow(page);
});

test("expired, revoked, and wrong bearer codes fail closed without echoing secrets", async ({ page }) => {
  await blockNextRoutePrefetch(page);
  const cases = [
    { code: expiredBearerCode, heading: "По приглашению", status: 401 },
    { code: revokedBearerCode, heading: "По приглашению", status: 403 },
    { code: wrongBearerCode, heading: "По приглашению", status: 401 }
  ] as const;

  for (const { code, heading, status } of cases) {
    const response = await page.goto(`/tournaments/${privateBearerSlug}?invite_code=${code}`);
    expect(response?.status()).toBe(200);
    await expect(page.locator("main").getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(code);
    expect(bearerWorkspaceRequests.at(-1)).toEqual({
      slug: privateBearerSlug,
      inviteCode: code,
      workspaceView: "detail"
    });
    expect(bearerResponseStatuses.at(-1)).toEqual({ code, status });
    expect(apiRequests.join("\n")).not.toContain(code);
  }

  expect(inviteClaimRequests).toBe(0);
});

test("duplicate or malformed bearer query values fail closed before the workspace read", async ({ page }) => {
  await blockNextRoutePrefetch(page);
  const duplicatePath = `/tournaments/${privateBearerSlug}?invite_code=${privateBearerCode}&invite_code=SECONDVALID26`;
  const malformedPath = `/tournaments/${privateBearerSlug}?invite_code=too-short`;

  await page.goto(duplicatePath);
  await expect(page.locator("main").getByRole("heading", { name: "По приглашению", exact: true })).toBeVisible();
  expect(bearerWorkspaceRequests.at(-1)).toEqual({
    slug: privateBearerSlug,
    inviteCode: null,
    workspaceView: "detail"
  });
  expect(bearerResponseStatuses.at(-1)).toEqual({ code: null, status: 401 });

  await page.goto(malformedPath);
  await expect(page.locator("main").getByRole("heading", { name: "По приглашению", exact: true })).toBeVisible();
  expect(bearerWorkspaceRequests.at(-1)).toEqual({
    slug: privateBearerSlug,
    inviteCode: null,
    workspaceView: "detail"
  });
  expect(bearerResponseStatuses.at(-1)).toEqual({ code: null, status: 401 });

  expect(apiRequests.join("\n")).not.toContain(privateBearerCode);
  expect(apiRequests.join("\n")).not.toContain("SECONDVALID26");
  expect(apiRequests.join("\n")).not.toContain("too-short");
  expect(inviteClaimRequests).toBe(0);
});

async function expectWorkspaceRequest(expected: typeof workspaceRequests[number]) {
  await expect.poll(() => workspaceRequests).toEqual([expected]);
}

function workspacePayload(
  slug: string,
  authenticated: boolean,
  includeCurrentUser: boolean,
  inviteCode: string | null = null,
  inactiveParticipant = false
) {
  const isReady = slug === readyTournamentSlug;
  const isPrivateBearer = slug === privateBearerSlug && inviteCode === privateBearerCode;
  const currentUser = authenticated && includeCurrentUser ? platformUser() : null;
  type WorkspaceTournamentFixture = Pick<
    PlatformTournament,
    "id" | "slug" | "name" | "description" | "visibility" | "status"
      | "format_slug" | "organizer_user_id" | "organizer_display_name"
      | "participant_count" | "max_participants" | "current_user_participant_status"
      | "starts_at" | "registration_closes_at" | "ready_check_starts_at"
      | "ready_check_ends_at" | "captain_selection_starts_at" | "allowed_ranks"
  > & { invite_code?: string | null };
  const tournament: WorkspaceTournamentFixture = {
    id: `t_${slug}`,
    slug,
    name: isReady ? "Lean Ready Cup" : "Lean Detail Cup",
    description: "Lean tournament detail smoke tournament.",
    visibility: isPrivateBearer ? "private" : "public",
    status: isReady ? "registration_closed" : "registration_open",
    format_slug: "solo",
    organizer_user_id: "user-organizer",
    organizer_display_name: "Roster Organizer",
    participant_count: 26,
    max_participants: 64,
    current_user_participant_status: isPrivateBearer
      ? (inactiveParticipant ? "withdrawn" : null)
      : authenticated ? "registered" : null,
    starts_at: "2026-07-20T18:00:00Z",
    registration_closes_at: "2026-07-20T16:00:00Z",
    ready_check_starts_at: "2026-07-20T16:05:00Z",
    ready_check_ends_at: "2026-07-20T16:20:00Z",
    captain_selection_starts_at: "2026-07-20T16:30:00Z",
    allowed_ranks: ["r1", "r2"],
    invite_code: isPrivateBearer ? privateBearerCode : null
  };
  const bracket = isPrivateBearer ? {
    tournament_id: `t_${slug}`,
    tournament_status: "in_progress",
    status: "ready",
    revision: 1,
    can_manage: false,
    capabilities: {
      can_manage: false,
      can_schedule_matches: false,
      can_report_matches: false
    },
    teams: [],
    matches: [{
      id: `m_${slug}`,
      round_number: 1,
      match_order: 1,
      team_a_id: null,
      team_b_id: null,
      home_label: "Alpha",
      away_label: "Beta",
      score_a: null,
      score_b: null,
      winner_team_id: null,
      home_source_match_id: null,
      away_source_match_id: null,
      status: "scheduled",
      match_format: "bo1",
      ready: true,
      scheduled_at: null
    }]
  } : {
    tournament_id: `t_${slug}`,
    status: "pending",
    revision: 0,
    can_manage: false,
    teams: [],
    matches: []
  };
  return {
    tournament,
    server_time: "2026-07-20T16:10:00Z",
    current_user: currentUser,
    participants: [],
    participants_total: 26,
    participants_limit: 0,
    participants_offset: 0,
    participants_has_more: true,
    participants_available: true,
    bracket,
    ready_check: isReady ? {
      active_round: readyRound(null),
      latest_round: readyRound(null)
    } : null,
    auto_assignment: null
  };
}

function privateBearerStatus(inviteCode: string | null): number | null {
  if (inviteCode === privateBearerCode) {
    return null;
  }
  if (inviteCode === expiredBearerCode || inviteCode === null) {
    return 401;
  }
  if (inviteCode === revokedBearerCode) {
    return 403;
  }
  // Any other code for an existing private tournament fails closed as an
  // anonymous unauthorized request; only an unknown tournament is 404.
  return 401;
}

function readyRound(choice: string | null) {
  return {
    id: 101,
    tournament_id: `t_${readyTournamentSlug}`,
    status: "active",
    eligible_participant_count: 26,
    ready_count: choice === "yes" ? 1 : 0,
    declined_count: 0,
    initiated_by_user_id: "user-organizer",
    created_at: "2026-07-20T16:05:00Z",
    closed_at: null,
    current_user_choice: choice
  };
}

function readyVoteResponse(choice: string) {
  return {
    round_id: 101,
    tournament_id: `t_${readyTournamentSlug}`,
    status: "active",
    eligible_participant_count: 26,
    current_user_choice: choice,
    changed: true,
    server_received_at: "2026-07-20T16:06:00Z"
  };
}

function platformUser() {
  return {
    id: actorUserId,
    email: "actor@example.test",
    display_name: "Actor Player",
    status: "active",
    created_at: "2026-05-01T12:00:00Z",
    roles: ["authenticated_user", "player"],
    can_create_public_tournaments: true
  };
}

function platformAuthBootstrap() {
  return {
    ...platformUser(),
    public_tournament_credits: 2,
    private_tournament_credits: 1,
    avatar_url: null,
    avatar_media: null
  };
}

function sanitizedRequestLog(method: string, url: URL): string {
  const params = new URLSearchParams(url.searchParams);
  if (params.has("invite_code")) {
    params.set("invite_code", "[redacted]");
  }
  const query = params.toString();
  return `${method} ${url.pathname}${query ? `?${query}` : ""}`;
}

function respondJson(
  response: ServerResponse,
  status: number,
  payload: unknown,
  headers: Record<string, string> = {}
) {
  response.writeHead(status, {
    "Content-Type": "application/json",
    ...headers
  });
  response.end(JSON.stringify(payload));
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  const overflow = await page.evaluate(() => (
    Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
  ));
  expect(overflow).toBeLessThanOrEqual(2);
}

async function blockNextRoutePrefetch(page: import("@playwright/test").Page) {
  await page.route("**/*", async (route) => {
    const headers = route.request().headers();
    if (headers["next-router-prefetch"] === "1" || headers.purpose === "prefetch") {
      await route.abort();
      return;
    }
    await route.continue();
  });
}
