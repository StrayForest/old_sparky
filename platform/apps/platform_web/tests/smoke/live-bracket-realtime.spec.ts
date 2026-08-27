import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

type GateState = {
  marker: string;
  origin: string;
  slug: string;
  organizer_email: string;
  watcher_email: string;
  password: string;
  initial_revision: number;
};

const gateDir = process.env.PLATFORM_QA_BROWSER_GATE_DIR;

test.skip(!gateDir, "PLATFORM_QA_BROWSER_GATE_DIR is required.");

test("authenticated bracket pans and refreshes after a cheap revision probe", async ({ browser }) => {
  const statePath = newestStatePath(gateDir!);
  const state = JSON.parse(fs.readFileSync(statePath, "utf8")) as GateState;
  const resultPath = path.join(gateDir!, `result-${state.marker}.json`);
  const organizerContext = await browser.newContext();
  const watcherContext = await browser.newContext();

  try {
    await Promise.all([
      organizerContext.addInitScript(() => {
        Math.random = () => 0;
      }),
      watcherContext.addInitScript(() => {
        Math.random = () => 0;
      }),
    ]);
    await login(organizerContext.request, state, state.organizer_email);
    await login(watcherContext.request, state, state.watcher_email);
    const organizerPage = await organizerContext.newPage();
    const watcherPage = await watcherContext.newPage();
    const route = `${state.origin}/tournaments/${state.slug}/bracket`;
    await Promise.all([
      organizerPage.goto(route),
      watcherPage.goto(route),
    ]);
    await expect(organizerPage.locator(".bracket-toolbar")).toHaveCount(0);
    await expect(watcherPage.locator(".bracket-toolbar")).toHaveCount(0);
    await expect(organizerPage.locator(".team-drag-handle")).toHaveCount(0);
    await expect(watcherPage.locator(".team-drag-handle")).toHaveCount(0);
    await expect(organizerPage.getByLabel("Управление масштабом сетки")).toBeVisible();
    const panDistance = await panBracket(organizerPage);
    const latencies = await triggerAndMeasureProbeRefresh(
      organizerContext,
      organizerPage,
      watcherPage,
      state,
    );

    fs.writeFileSync(
      resultPath,
      JSON.stringify({
        ok: true,
        pan_distance: panDistance,
        pointer_seconds: latencies.organizerSeconds,
        keyboard_seconds: latencies.watcherSeconds,
        organizer_event_seconds: latencies.organizerSeconds,
        watcher_event_seconds: latencies.watcherSeconds,
        observed_revision: latencies.revision,
      }),
      "utf8",
    );
  } catch (error) {
    fs.writeFileSync(
      resultPath,
      JSON.stringify({ ok: false, error: error instanceof Error ? error.message : String(error) }),
      "utf8",
    );
    throw error;
  } finally {
    await organizerContext.close();
    await watcherContext.close();
  }
});

async function login(
  request: import("@playwright/test").APIRequestContext,
  state: GateState,
  email: string,
) {
  const response = await request.post(`${state.origin}/api/v1/auth/login`, {
    data: { email, password: state.password },
  });
  expect(response.ok()).toBeTruthy();
}

type BracketState = {
  revision: number;
  matches: Array<{
    id: string;
    round_number: number;
  }>;
};

async function triggerAndMeasureProbeRefresh(
  organizerContext: import("@playwright/test").BrowserContext,
  organizerPage: import("@playwright/test").Page,
  watcherPage: import("@playwright/test").Page,
  state: GateState,
) {
  const bracketUrl = `${state.origin}/api/v1/tournaments/${state.slug}/bracket?teams_view=summary`;
  const bracketResponse = await organizerContext.request.get(bracketUrl);
  expect(bracketResponse.ok()).toBeTruthy();
  const bracket = await bracketResponse.json() as BracketState;
  expect(bracket.revision).toBe(state.initial_revision);
  const openingMatch = bracket.matches.find((match) => match.round_number === 1);
  expect(openingMatch).toBeTruthy();
  const nextRevision = bracket.revision + 1;

  const organizerRefresh = waitForRevision(organizerPage, state.slug, nextRevision);
  const watcherRefresh = waitForRevision(watcherPage, state.slug, nextRevision);
  const startedAt = performance.now();
  const organizerLatency = organizerRefresh.then(() => (performance.now() - startedAt) / 1000);
  const watcherLatency = watcherRefresh.then(() => (performance.now() - startedAt) / 1000);

  const csrfResponse = await organizerContext.request.get(
    `${state.origin}/api/v1/auth/csrf`,
  );
  expect(csrfResponse.ok()).toBeTruthy();
  const csrf = await csrfResponse.json() as { csrf_token: string };
  const scheduledAt = new Date(Date.now() + 60 * 60 * 1000).toISOString();
  const mutation = await organizerContext.request.patch(
    `${state.origin}/api/v1/tournaments/${state.slug}/matches/${openingMatch!.id}/schedule`,
    {
      data: {
        scheduled_at: scheduledAt,
        expected_revision: bracket.revision,
      },
      headers: { "X-CSRF-Token": csrf.csrf_token },
    },
  );
  expect(mutation.status()).toBe(200);
  const [organizerSeconds, watcherSeconds] = await Promise.all([
    organizerLatency,
    watcherLatency,
  ]);
  return { organizerSeconds, watcherSeconds, revision: nextRevision };
}

async function waitForRevision(
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
  }, { timeout: 25_000 });
}

async function panBracket(page: import("@playwright/test").Page) {
  const shell = page.locator(".bracket-shell");
  await expect(shell).toBeVisible();
  for (let index = 0; index < 6; index += 1) {
    await page.getByLabel("Приблизить").click();
  }
  const box = await shell.boundingBox();
  expect(box).not.toBeNull();
  const before = await scrollDistance(shell);
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 - 180, box!.y + box!.height / 2 - 140, { steps: 16 });
  await page.mouse.up();
  await expect.poll(() => scrollDistance(shell), { timeout: 2_000 }).toBeGreaterThan(before);
  return await scrollDistance(shell);
}

async function scrollDistance(shell: import("@playwright/test").Locator) {
  return shell.evaluate((node) => node.scrollLeft + node.scrollTop);
}

function newestStatePath(directory: string) {
  const entries = fs.readdirSync(directory)
    .filter((entry) => entry.startsWith("state-") && entry.endsWith(".json"))
    .map((entry) => ({
      path: path.join(directory, entry),
      mtime: fs.statSync(path.join(directory, entry)).mtimeMs,
    }))
    .sort((left, right) => right.mtime - left.mtime);
  if (!entries[0]) {
    throw new Error(`No QA browser gate state found in ${directory}.`);
  }
  return entries[0].path;
}
