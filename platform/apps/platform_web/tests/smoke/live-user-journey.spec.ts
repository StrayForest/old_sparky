import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { validateLiveQaOrigin } from "../support/live-qa-origin";
import { assertLiveQaChromiumSandbox } from "../support/live-qa-sandbox";

const enabled = process.env.PLATFORM_LIVE_USER_QA === "1";
const qaRunnerUid = enabled ? requireQaRunnerUid() : null;
const marker = process.env.PLATFORM_LIVE_USER_QA_MARKER ?? `liveqa-${Date.now()}`;
const origin = enabled
  ? validateLiveQaOrigin({
    allowLoopback: process.env.PLATFORM_LIVE_CSP_ALLOW_LOOPBACK === "1",
    configured: process.env.PLAYWRIGHT_LIVE_BASE_URL,
    expected: process.env.PLATFORM_LIVE_EXPECTED_ORIGIN,
  })
  : (process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1").replace(/\/$/, "");
const csrfTokens = new WeakMap<import("@playwright/test").BrowserContext, string>();
const cspViolationConsolePrefix = "__platform_liveqa_csp_violation__:";
const reportOnlyFrameAncestorsWarning =
  "The Content Security Policy directive 'frame-ancestors' is ignored when delivered in a report-only policy.";

test.skip(!enabled, "PLATFORM_LIVE_USER_QA=1 is required.");
test.setTimeout(600_000);

test("live player completes the accelerated tournament journey through visible controls", async ({ browser }, testInfo) => {
  const sessions = loadQaSessions();
  const inventory = loadInventory(sessions);
  const helperContexts: import("@playwright/test").BrowserContext[] = [];
  const authenticatedRosterContexts: import("@playwright/test").BrowserContext[] = [];
  const organizerContext = await browser.newContext();
  const playerContext = await browser.newContext();
  organizerContext.setDefaultTimeout(15_000);
  playerContext.setDefaultTimeout(15_000);
  const browserGateFailures: string[] = [];
  const confirmedBrowserMutations = new Set<string>();
  await Promise.all([
    installBrowserGate(
      organizerContext,
      "organizer",
      browserGateFailures,
      confirmedBrowserMutations,
    ),
    installBrowserGate(
      playerContext,
      "player",
      browserGateFailures,
      confirmedBrowserMutations,
    ),
  ]);
  await assertLiveQaChromiumSandbox(organizerContext, browser.browserType().name());
  const tournamentName = `QA ${marker.slice(-20)}`;
  let tournamentSlug = "";

  try {
    authenticatedRosterContexts.push(organizerContext);
    await authenticateQaContext(
      organizerContext,
      sessions.cookieName,
      sessions.rosterSessions[0],
      sessions.expiresAt,
    );
    const helpers: import("@playwright/test").BrowserContext[] = [];
    for (let index = 0; index < 12; index += 1) {
      const context = await browser.newContext();
      helperContexts.push(context);
      helpers.push(context);
      await installBrowserGate(
        context,
        `roster-${index + 1}`,
        browserGateFailures,
        confirmedBrowserMutations,
      );
      authenticatedRosterContexts.push(context);
      await authenticateQaContext(
        context,
        sessions.cookieName,
        sessions.rosterSessions[index + 1],
        sessions.expiresAt,
      );
    }
    await authenticateQaContext(
      playerContext,
      sessions.cookieName,
      sessions.workflowPlayer,
      sessions.expiresAt,
    );

    const coverPage = await organizerContext.newPage();
    await coverPage.goto(`${origin}/tournaments/new`);
    await coverPage.getByLabel("Загрузить обложку турнира").setInputFiles({
      name: `${marker}-cover.png`,
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lq2ZAAAAAElFTkSuQmCC",
        "base64",
      ),
    });
    const coverPreview = coverPage.locator(".cover-box .cover-preview-image");
    await expect(coverPreview).toHaveAttribute("src", /^blob:/u);
    await expect(coverPreview).toHaveCSS("object-fit", "cover");
    await coverPage.close();

    const playerPage = await playerContext.newPage();
    await playerPage.goto(`${origin}/profile/me`);
    await expect(playerPage).toHaveURL(/\/profile\/me$/);
    await screenshot(playerPage, testInfo, "01-workflow-player-ready");

    await playerPage.getByTestId("profile-pill-phantom").click();
    await playerPage.getByTestId("profile-pill-vi").click();
    await playerPage.getByTestId("profile-pill-1501-2000").click();
    await playerPage.getByTestId("profile-pill-carry").click();
    await playerPage.getByRole("button", { name: "Abrams", exact: true }).click();
    await playerPage.getByRole("button", { name: "Apollo", exact: true }).click();
    await playerPage.getByRole("button", { name: "Bebop", exact: true }).click();
    const profileUpdateResponse = playerPage.waitForResponse((response) => (
      response.request().method() === "PUT"
      && new URL(response.url()).pathname === "/api/v1/profiles/me/deadlock"
    ));
    await playerPage.getByTestId("profile-save-settings-button").click();
    expect((await profileUpdateResponse).status()).toBe(200);
    await expect(playerPage.getByTestId("profile-save-settings-button")).toHaveText(/Сохранено/);
    confirmedBrowserMutations.add("PUT:/api/v1/profiles/me/deadlock");
    await screenshot(playerPage, testInfo, "02-profile-complete");

    const created = await apiJson<{ id: string; slug: string }>(
      organizerContext,
      "/tournaments",
      "POST",
      {
        name: tournamentName,
        description: `Accelerated live browser acceptance ${marker}.`,
        visibility: "invite_only",
        format_slug: "solo",
        allowed_ranks: ["Eternus", "Ascendant", "Phantom", "Oracle", "Emissary", "Ritualist"],
        max_participants: 14,
        match_format: "bo1",
        final_format: "bo1",
        teams_count: 2
      },
      201
    );
    recordInventoryId(inventory, "tournament_ids", created.id);
    tournamentSlug = created.slug;
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/status`, "PATCH", { status: "registration_open" }, 200);
    const invite = await apiJson<{ code: string }>(
      organizerContext,
      `/tournaments/${tournamentSlug}/invites`,
      "POST",
      { note: marker, max_uses: 20, expires_at: null },
      201
    );

    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/join`, "POST", { entry_type: "solo" }, 201);
    for (const context of helpers) {
      await apiJson(context, "/tournaments/invites/claim", "POST", { code: invite.code, entry_type: "solo", team_name: null }, 201);
      await apiJson(context, `/tournaments/${tournamentSlug}/join`, "POST", { entry_type: "solo" }, 201);
    }

    await playerPage.goto(`${origin}/tournaments`);
    await playerPage.getByLabel("Код приглашения").fill(invite.code);
    await expect(playerPage).toHaveURL(new RegExp(`/tournaments/${tournamentSlug}$`));
    await playerPage.getByRole("button", { name: "Зарегистрироваться" }).click();
    await expect(playerPage.getByRole("button", { name: "Отменить регистрацию" })).toBeVisible();
    await expect(playerPage.getByText("Регистрация завершена", { exact: true })).toBeVisible();
    await screenshot(playerPage, testInfo, "03-registered");

    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/status`, "PATCH", { status: "registration_closed" }, 200);
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/ready-check/start`, "POST", undefined, 201);
    await playerPage.reload();
    await playerPage.getByRole("button", { name: "Подтвердить участие" }).click();
    await expect(playerPage.getByRole("button", { name: "Отменить подтверждение" })).toBeVisible();
    await screenshot(playerPage, testInfo, "04-ready-confirmed");

    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/ready-check/vote`, "POST", { choice: "yes" }, 200);
    for (const context of helpers) {
      await apiJson(context, `/tournaments/${tournamentSlug}/deadlock/ready-check/vote`, "POST", { choice: "yes" }, 200);
    }
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/ready-check/close`, "POST", undefined, 200);
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/captain-round/start`, "POST", { teams_count: 2 }, 201);

    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/auto-assignment/run-async`, "POST", undefined, 202);
    const run = await pollAssignment(organizerContext, tournamentSlug);
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/auto-assignment/${run.id}/publish`, "POST", undefined, 200);
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/deadlock/auto-assignment/${run.id}/lock`, "POST", undefined, 200);
    await apiJson(organizerContext, `/tournaments/${tournamentSlug}/matches/seed-opening-round`, "POST", undefined, 201);

    await playerPage.reload();
    await expect(playerPage.getByText("Моя команда", { exact: true })).toBeVisible();
    await Promise.all([
      organizerContext.addInitScript(() => { Math.random = () => 0; }),
      playerContext.addInitScript(() => { Math.random = () => 0; }),
    ]);
    const organizerPage = await organizerContext.newPage();
    const bracketRoute = `${origin}/tournaments/${tournamentSlug}/bracket`;
    const eventPath = `/api/v1/tournaments/${tournamentSlug}/bracket/events`;
    const organizerEvent = organizerPage.waitForRequest((request) => (
      new URL(request.url()).pathname === eventPath
    ));
    const playerEvent = playerPage.waitForRequest((request) => (
      new URL(request.url()).pathname === eventPath
    ));
    await Promise.all([
      organizerPage.goto(bracketRoute),
      playerPage.goto(bracketRoute),
      organizerEvent,
      playerEvent,
    ]);
    await expect(playerPage.getByTestId("bracket-shell")).toBeVisible();
    await expect(organizerPage.getByTestId("bracket-shell")).toBeVisible();
    await assertTwoContextSseRefresh(
      organizerContext,
      organizerPage,
      playerPage,
      tournamentSlug,
    );
    await screenshot(playerPage, testInfo, "05-player-bracket");

    const match = organizerPage.getByTestId("bracket-match").filter({ has: organizerPage.getByRole("button", { name: "ОК" }) }).first();
    await match.getByLabel("Счет хозяев").fill("1");
    await match.getByLabel("Счет гостей").fill("0");
    const scoreResponse = organizerPage.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname.startsWith(
        `/api/v1/tournaments/${tournamentSlug}/matches/`,
      )
      && new URL(response.url()).pathname.endsWith("/report")
    ));
    await match.getByRole("button", { name: "ОК" }).click();
    expect((await scoreResponse).status()).toBe(200);
    await expect(match.getByLabel("Счет хозяев")).toHaveValue("1");
    await expect(match.getByLabel("Счет гостей")).toHaveValue("0");
    await expect(match.getByLabel("Счет хозяев")).toBeDisabled();
    await expect(match.getByLabel("Счет гостей")).toBeDisabled();
    await screenshot(organizerPage, testInfo, "06-final-score");

    await expect.poll(async () => {
      const tournament = await apiJson<{ status: string }>(organizerContext, `/tournaments/${tournamentSlug}`, "GET", undefined, 200);
      return tournament.status;
    }, { timeout: 10_000 }).toBe("completed");
  } finally {
    const logoutResults = await Promise.allSettled(
      [
        ...authenticatedRosterContexts.map((context) => (
          logoutQaAccount(context, { allowUnauthenticated: true })
        )),
        logoutQaAccount(playerContext, { allowUnauthenticated: true }),
      ],
    );
    const confirmedProfileAbort = (
      `player:requestfailed:PUT:${origin}/api/v1/profiles/me/deadlock:net::ERR_ABORTED`
    );
    const browserGateSnapshot = browserGateFailures.filter((failure) => !(
      failure === confirmedProfileAbort
      && confirmedBrowserMutations.has("PUT:/api/v1/profiles/me/deadlock")
    ));
    await Promise.allSettled(helperContexts.map((context) => context.close()));
    await organizerContext.close();
    await playerContext.close();
    await testInfo.attach("live-user-browser-gate", {
      body: JSON.stringify({ failures: browserGateSnapshot }),
      contentType: "application/json",
    });
    const logoutFailures = logoutResults.filter((result) => result.status === "rejected");
    expect(
      logoutFailures,
      "every persistent roster and disposable player QA session must be invalidated",
    ).toHaveLength(0);
    expect(browserGateSnapshot, "live-user CSP/page/request browser failures").toEqual([]);
  }
});

type QaBrowserSession = {
  userId: string;
  sessionId: string;
  token: string;
};

type QaSessions = {
  marker: string;
  cookieName: string;
  expiresAt: number;
  rosterSessions: QaBrowserSession[];
  workflowPlayer: QaBrowserSession;
};

type QaInventory = {
  version: 1;
  marker: string;
  user_ids: string[];
  tournament_ids: string[];
  media_ids: string[];
};

async function installBrowserGate(
  context: import("@playwright/test").BrowserContext,
  label: string,
  failures: string[],
  confirmedMutations: Set<string>,
) {
  await context.addInitScript((prefix) => {
    if (window.top !== window) {
      return;
    }
    document.addEventListener("securitypolicyviolation", (event) => {
      console.warn(
        `${prefix}${event.disposition.slice(0, 16)}:${event.effectiveDirective.slice(0, 64)}`,
      );
    });
  }, cspViolationConsolePrefix);
  context.on("request", (request) => {
    try {
      const parsed = new URL(request.url());
      if (
        parsed.hostname === "challenges.cloudflare.com"
        || (
          parsed.origin === origin
          && ["/auth/login", "/auth/register", "/reset-password", "/verify-email"].includes(
            parsed.pathname,
          )
        )
      ) {
        failures.push(
          `${label}:forbidden-auth-automation:${request.method()}:${parsed.origin}${parsed.pathname}`,
        );
      }
    } catch {
      // Other request diagnostics already normalize malformed URLs below.
    }
  });
  context.on("page", (page) => {
    page.on("console", (message) => {
      if (message.text().startsWith(cspViolationConsolePrefix)) {
        failures.push(`${label}:csp:${message.text().slice(cspViolationConsolePrefix.length)}`);
        return;
      }
      if (
        message.type() === "error"
        && /content security policy|refused to (?:execute|apply|load|connect)/iu.test(
          message.text(),
        )
        && message.text() !== reportOnlyFrameAncestorsWarning
      ) {
        failures.push(`${label}:csp-console`);
      }
    });
    page.on("pageerror", (error) => {
      failures.push(`${label}:pageerror:${error.name || "unknown"}`);
    });
    page.on("requestfailed", (request) => {
      const errorText = request.failure()?.errorText ?? "failed";
      if (
        request.resourceType() === "eventsource"
        && /abort|cancel/iu.test(errorText)
      ) {
        return;
      }
      let requestPath = "invalid";
      try {
        const parsed = new URL(request.url());
        const headers = request.headers();
        const isExpectedNextPrefetchAbort = (
          errorText === "net::ERR_ABORTED"
          && request.method() === "GET"
          && parsed.origin === origin
          && (
            parsed.searchParams.has("_rsc")
            || headers.rsc === "1"
            || headers["next-router-prefetch"] === "1"
            || headers["next-router-segment-prefetch"] === "1"
          )
        );
        const isExpectedNavigationApiCancellation = (
          errorText === "Load request cancelled"
          && request.method() === "GET"
          && parsed.origin === origin
          && [
            "/api/v1/content/support/status",
            "/api/v1/users/me",
          ].includes(parsed.pathname)
        );
        const isConfirmedMutationTeardown = (
          errorText === "net::ERR_ABORTED"
          && parsed.origin === origin
          && confirmedMutations.has(`${request.method()}:${parsed.pathname}`)
        );
        if (
          isExpectedNextPrefetchAbort
          || isExpectedNavigationApiCancellation
          || isConfirmedMutationTeardown
        ) {
          return;
        }
        requestPath = `${parsed.origin}${parsed.pathname}`;
      } catch {
        // Keep hostile or malformed URLs out of the test artifact.
      }
      const safeError = /^(?:net::[A-Z_]+|Load request cancelled)$/u.test(errorText)
        ? errorText
        : "failed";
      failures.push(`${label}:requestfailed:${request.method()}:${requestPath}:${safeError}`);
    });
  });
}

function loadQaSessions(): QaSessions {
  const configuredPath = process.env.PLATFORM_LIVE_USER_QA_SESSIONS;
  if (!configuredPath || !path.isAbsolute(configuredPath)) {
    throw new Error("PLATFORM_LIVE_USER_QA_SESSIONS must be an absolute path.");
  }
  assertQaRunnerStateParent(configuredPath);
  const metadata = fs.lstatSync(configuredPath);
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error("The live-user session fixture must be a regular file.");
  }
  if (metadata.uid !== qaRunnerUid || (metadata.mode & 0o777) !== 0o600 || metadata.size > 64 * 1024) {
    throw new Error("The live-user session fixture must be runner-owned 0600 and bounded.");
  }
  const raw = fs.readFileSync(configuredPath, "utf8");
  const payload = JSON.parse(raw) as Record<string, unknown>;
  if (
    !hasExactKeys(payload, [
      "version",
      "marker",
      "cookie_name",
      "created_at",
      "expires_at",
      "roster_sessions",
      "workflow_player",
    ])
    || payload.version !== 1
    || payload.marker !== marker
    || typeof payload.cookie_name !== "string"
    || !/^__Host-[A-Za-z0-9_-]{1,96}$/u.test(payload.cookie_name)
    || typeof payload.created_at !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(payload.created_at)
    || typeof payload.expires_at !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(payload.expires_at)
    || !Array.isArray(payload.roster_sessions)
    || payload.roster_sessions.length !== 13
  ) {
    throw new Error("The live-user session fixture has an unexpected schema.");
  }
  const createdAt = Date.parse(payload.created_at);
  const expiresAt = Date.parse(payload.expires_at);
  const currentTime = Date.now();
  if (
    !Number.isFinite(createdAt)
    || !Number.isFinite(expiresAt)
    || createdAt > currentTime + 60_000
    || createdAt < currentTime - 10 * 60 * 1000
    || expiresAt <= currentTime + 5 * 60 * 1000
    || expiresAt <= createdAt
    || expiresAt - createdAt > 60 * 60 * 1000
  ) {
    throw new Error("The live-user session fixture has an unsafe lifetime.");
  }
  const parseSession = (value: unknown, label: string): QaBrowserSession => {
    if (!value || typeof value !== "object") {
      throw new Error(`${label} must be an object.`);
    }
    const candidate = value as Record<string, unknown>;
    if (
      !hasExactKeys(candidate, ["user_id", "session_id", "token"])
      || typeof candidate.user_id !== "string"
      || !isCanonicalUuid(candidate.user_id)
      || typeof candidate.session_id !== "string"
      || !isCanonicalUuid(candidate.session_id)
      || typeof candidate.token !== "string"
      || !/^[A-Za-z0-9_-]{64,128}$/u.test(candidate.token)
    ) {
      throw new Error(`${label} has an invalid session schema.`);
    }
    return {
      userId: candidate.user_id,
      sessionId: candidate.session_id,
      token: candidate.token,
    };
  };
  const rosterSessions = payload.roster_sessions.map((value, index) => (
    parseSession(value, `roster_sessions[${index}]`)
  ));
  const workflowPlayer = parseSession(payload.workflow_player, "workflow_player");
  const allSessions = [...rosterSessions, workflowPlayer];
  if (
    new Set(allSessions.map((value) => value.userId)).size !== 14
    || new Set(allSessions.map((value) => value.sessionId)).size !== 14
    || new Set(allSessions.map((value) => value.token)).size !== 14
  ) {
    throw new Error("The live-user session fixture identities must be unique.");
  }
  return {
    marker,
    cookieName: payload.cookie_name,
    expiresAt,
    rosterSessions,
    workflowPlayer,
  };
}

function isCanonicalUuid(value: string): boolean {
  return /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u.test(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length
    && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function loadInventory(sessions: QaSessions): QaInventory {
  if (!/^liveqa-[a-z0-9-]{6,56}$/u.test(marker)) {
    throw new Error("PLATFORM_LIVE_USER_QA_MARKER has an invalid liveqa marker.");
  }
  const target = inventoryPath();
  assertQaRunnerStateParent(target);
  const metadata = fs.lstatSync(target);
  if (
    metadata.isSymbolicLink()
    || !metadata.isFile()
    || metadata.uid !== qaRunnerUid
    || (metadata.mode & 0o777) !== 0o600
    || metadata.size > 64 * 1024
  ) {
    throw new Error("The preseeded QA inventory must be a runner-owned 0600 regular file.");
  }
  const payload = JSON.parse(fs.readFileSync(target, "utf8")) as Record<string, unknown>;
  if (
    !hasExactKeys(payload, [
      "version",
      "marker",
      "user_ids",
      "tournament_ids",
      "media_ids",
    ])
    || payload.version !== 1
    || payload.marker !== marker
    || !Array.isArray(payload.user_ids)
    || !Array.isArray(payload.tournament_ids)
    || !Array.isArray(payload.media_ids)
    || payload.tournament_ids.length !== 0
    || payload.media_ids.length !== 0
    || payload.user_ids.length !== 14
    || payload.user_ids.some((value) => typeof value !== "string" || !isCanonicalUuid(value))
  ) {
    throw new Error("The preseeded QA inventory has an unexpected schema.");
  }
  const expectedIds = new Set([
    ...sessions.rosterSessions.map((session) => session.userId),
    sessions.workflowPlayer.userId,
  ]);
  const actualIds = new Set(payload.user_ids as string[]);
  if (
    actualIds.size !== 14
    || [...actualIds].some((id) => !expectedIds.has(id))
  ) {
    throw new Error("The preseeded QA inventory does not exactly match the browser sessions.");
  }
  return payload as QaInventory;
}

function inventoryPath(): string {
  const configuredPath = process.env.PLATFORM_LIVE_USER_QA_INVENTORY;
  if (!configuredPath || !path.isAbsolute(configuredPath)) {
    throw new Error("PLATFORM_LIVE_USER_QA_INVENTORY must be an absolute path.");
  }
  return configuredPath;
}

function requireQaRunnerUid(): number {
  const configured = process.env.PLATFORM_LIVE_USER_QA_UID;
  if (!configured || !/^[1-9][0-9]{0,9}$/u.test(configured)) {
    throw new Error("PLATFORM_LIVE_USER_QA_UID must identify a non-root runner.");
  }
  const expectedUid = Number(configured);
  if (
    !Number.isSafeInteger(expectedUid)
    || typeof process.geteuid !== "function"
    || process.geteuid() === 0
    || process.geteuid() !== expectedUid
  ) {
    throw new Error("Live-user Playwright must run as the exact non-root QA runner.");
  }
  return expectedUid;
}

function assertQaRunnerStateParent(target: string) {
  const parent = path.dirname(target);
  const metadata = fs.lstatSync(parent);
  if (
    metadata.isSymbolicLink()
    || !metadata.isDirectory()
    || metadata.uid !== qaRunnerUid
    || (metadata.mode & 0o777) !== 0o700
    || fs.realpathSync(parent) !== parent
  ) {
    throw new Error("The live-user QA state directory must be runner-owned 0700 without symlinks.");
  }
}

function writeInventory(inventory: QaInventory) {
  const target = inventoryPath();
  const metadata = fs.lstatSync(target);
  if (
    metadata.isSymbolicLink()
    || !metadata.isFile()
    || metadata.uid !== qaRunnerUid
    || (metadata.mode & 0o777) !== 0o600
  ) {
    throw new Error("Refusing to replace an unsafe QA inventory.");
  }
  const parent = path.dirname(target);
  const temporary = path.join(
    parent,
    `.${path.basename(target)}.${randomUUID()}.tmp`,
  );
  let descriptor: number | undefined;
  try {
    descriptor = fs.openSync(
      temporary,
      fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
      0o600,
    );
    fs.fchmodSync(descriptor, 0o600);
    fs.writeFileSync(descriptor, `${JSON.stringify(inventory)}\n`, "utf8");
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporary, target);
    const parentDescriptor = fs.openSync(
      parent,
      fs.constants.O_RDONLY | fs.constants.O_DIRECTORY,
    );
    try {
      fs.fsyncSync(parentDescriptor);
    } finally {
      fs.closeSync(parentDescriptor);
    }
  } finally {
    if (descriptor !== undefined) {
      fs.closeSync(descriptor);
    }
    if (fs.existsSync(temporary)) {
      fs.unlinkSync(temporary);
    }
  }
}

function recordInventoryId(
  inventory: QaInventory,
  field: "tournament_ids" | "media_ids",
  id: string,
) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(id)) {
    throw new Error(`The ${field} response did not contain a canonical UUID.`);
  }
  if (!inventory[field].includes(id)) {
    inventory[field].push(id);
    writeInventory(inventory);
  }
}

async function authenticateQaContext(
  context: import("@playwright/test").BrowserContext,
  cookieName: string,
  session: QaBrowserSession,
  expiresAt: number,
) {
  await context.addCookies([{
    name: cookieName,
    value: session.token,
    url: origin,
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
    expires: Math.floor(expiresAt / 1000),
  }]);
  const response = await context.request.get(`${origin}/api/v1/auth/session`);
  if (response.status() !== 200) {
    throw new Error("The short-lived live-user session fixture was rejected.");
  }
  const payload = await response.json() as { id?: unknown };
  if (payload.id !== session.userId) {
    throw new Error("The live-user session resolved to an unexpected user.");
  }
}

async function logoutQaAccount(
  context: import("@playwright/test").BrowserContext,
  options: { allowUnauthenticated?: boolean } = {},
) {
  const csrfResponse = await context.request.get(`${origin}/api/v1/auth/csrf`);
  if (options.allowUnauthenticated && csrfResponse.status() === 401) {
    return;
  }
  if (csrfResponse.status() !== 200) {
    throw new Error("Roster QA logout could not obtain a CSRF token.");
  }
  const csrf = await csrfResponse.json() as { csrf_token?: unknown };
  if (typeof csrf.csrf_token !== "string") {
    throw new Error("Roster QA logout received an invalid CSRF response.");
  }
  const logoutResponse = await context.request.post(`${origin}/api/v1/auth/logout`, {
    headers: {
      Origin: origin,
      "X-CSRF-Token": csrf.csrf_token,
    },
  });
  if (
    logoutResponse.status() !== 204
    && !(options.allowUnauthenticated && logoutResponse.status() === 401)
  ) {
    throw new Error(`Roster QA logout returned unexpected status ${logoutResponse.status()}.`);
  }
  if (logoutResponse.status() === 204) {
    const sessionResponse = await context.request.get(`${origin}/api/v1/auth/session`);
    if (sessionResponse.status() !== 401) {
      throw new Error("Roster QA logout left an authenticated session active.");
    }
  }
}

async function assertTwoContextSseRefresh(
  organizerContext: import("@playwright/test").BrowserContext,
  organizerPage: import("@playwright/test").Page,
  playerPage: import("@playwright/test").Page,
  slug: string,
) {
  const bracket = await apiJson<{
    revision: number;
    matches: Array<{ id: string; round_number: number }>;
  }>(organizerContext, `/tournaments/${slug}/bracket?teams_view=summary`, "GET", undefined, 200);
  const openingMatch = bracket.matches.find((match) => match.round_number === 1);
  expect(openingMatch).toBeTruthy();
  const nextRevision = bracket.revision + 1;
  const organizerRefresh = waitForBracketRevision(organizerPage, slug, nextRevision);
  const playerRefresh = waitForBracketRevision(playerPage, slug, nextRevision);
  const startedAt = performance.now();
  const organizerLatency = organizerRefresh.then(() => (performance.now() - startedAt) / 1000);
  const playerLatency = playerRefresh.then(() => (performance.now() - startedAt) / 1000);

  await apiJson(
    organizerContext,
    `/tournaments/${slug}/matches/${openingMatch!.id}/schedule`,
    "PATCH",
    {
      scheduled_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      expected_revision: bracket.revision,
    },
    200,
  );
  const [organizerSeconds, playerSeconds] = await Promise.all([
    organizerLatency,
    playerLatency,
  ]);
  expect(organizerSeconds).toBeLessThanOrEqual(2);
  expect(playerSeconds).toBeLessThanOrEqual(2);
}

async function waitForBracketRevision(
  page: import("@playwright/test").Page,
  slug: string,
  revision: number,
) {
  const pathName = `/api/v1/tournaments/${slug}/bracket`;
  return page.waitForResponse(async (response) => {
    if (
      response.request().method() !== "GET"
      || new URL(response.url()).pathname !== pathName
      || response.status() !== 200
    ) {
      return false;
    }
    const payload = await response.json().catch(() => null) as { revision?: number } | null;
    return Number(payload?.revision ?? -1) >= revision;
  }, { timeout: 2_000 });
}

async function csrfToken(context: import("@playwright/test").BrowserContext) {
  const cached = csrfTokens.get(context);
  if (cached) {
    return cached;
  }
  const response = await context.request.get(`${origin}/api/v1/auth/csrf`);
  if (response.status() !== 200) {
    throw new Error("Live-user QA could not obtain a CSRF token.");
  }
  const payload = await response.json() as { csrf_token?: unknown };
  if (typeof payload.csrf_token !== "string") {
    throw new Error("Live-user QA received an invalid CSRF token response.");
  }
  csrfTokens.set(context, payload.csrf_token);
  return payload.csrf_token;
}

async function apiJson<T = Record<string, unknown>>(
  context: import("@playwright/test").BrowserContext,
  path: string,
  method: string,
  data: unknown,
  expected: number
): Promise<T> {
  const headers: Record<string, string> = {
    "X-Platform-QA-Phase": `live_user_${marker}`,
  };
  if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) {
    headers.Origin = origin;
    headers["X-CSRF-Token"] = await csrfToken(context);
  }
  const response = await context.request.fetch(`${origin}/api/v1${path}`, {
    method,
    data,
    headers,
  });
  if (response.status() !== expected) {
    throw new Error(
      `Live-user QA API status mismatch: ${method.toUpperCase()} ${path} expected ${expected}, got ${response.status()}.`
    );
  }
  return response.status() === 204 ? ({} as T) : await response.json() as T;
}

async function pollAssignment(context: import("@playwright/test").BrowserContext, slug: string) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const state = await apiJson<{ latest_run: { id: string; status: string; teams: unknown[] } | null }>(
      context,
      `/tournaments/${slug}/deadlock/auto-assignment`,
      "GET",
      undefined,
      200
    );
    if (
      state.latest_run
      && ["generated", "draft"].includes(state.latest_run.status)
      && state.latest_run.teams.length === 2
    ) {
      return state.latest_run;
    }
    if (state.latest_run && ["failed", "cancelled"].includes(state.latest_run.status)) {
      throw new Error(`Assignment ended with status ${state.latest_run.status}.`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("Timed out waiting for a two-team assignment.");
}

async function screenshot(
  page: import("@playwright/test").Page,
  testInfo: import("@playwright/test").TestInfo,
  name: string
) {
  if (testInfo.project.name === "live-webkit-mobile") {
    return;
  }
  await page.screenshot({
    animations: "allow",
    caret: "initial",
    fullPage: true,
    path: testInfo.outputPath(`${name}.png`),
  });
}