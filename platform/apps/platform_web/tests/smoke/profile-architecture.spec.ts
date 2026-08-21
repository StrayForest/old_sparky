import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import {
  captainPreferenceFromApi,
  mapProfileWorkspacePayload,
  parseProfileTab,
} from "../../lib/profile-model";

type DreamSlotPayload = {
  slot_number: number;
  allowed_roles: string[];
  desired_heroes: string[];
};

type CaptainPayload = {
  captain_team_name: string;
  slots: DreamSlotPayload[];
};

async function authenticateTestUser(
  page: Page,
  extraCookies: Array<{ name: string; value: string }> = []
) {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100",
    },
    ...extraCookies.map((cookie) => ({
      ...cookie,
      url: "http://127.0.0.1:3100",
    })),
  ]);
}

test("profile workspace model preserves captain priority and normalizes six dream slots", () => {
  const workspace = mapProfileWorkspacePayload({
    profile: {
      user_id: "u_profile",
      account_email: "profile@example.test",
      display_name: "Profile",
      handle: "profile",
      avatar_url: null,
      banner_url: null,
      avatar_media: null,
      banner_media: null,
      bio: null,
      contact_email: "profile@example.test",
      region: "EU",
      steam_id: null,
      discord_account: null,
      captain_team_name: "Alpha",
      updated_at: "2026-08-18T00:00:00Z",
    },
    deadlock_profile: {
      user_id: "u_profile",
      rank: "Oracle",
      subrank: 4,
      playtime: "1001-1500",
      roles: ["Carry"],
      pool: ["Abrams", "Haze", "Ivy"],
      captain_priority: "yes",
      updated_at: "2026-08-18T00:00:00Z",
    },
    dream_slots: [
      {
        user_id: "u_profile",
        slot_number: 3,
        allowed_roles: ["Support"],
        desired_heroes: ["Ivy"],
        updated_at: null,
      },
    ],
  });

  expect(workspace.captainPreference).toBe("Повысить");
  expect(workspace.dreamSlots).toHaveLength(6);
  expect(workspace.dreamSlots[2]).toMatchObject({
    slot_number: 3,
    allowed_roles: ["Support"],
    desired_heroes: ["Ivy"],
  });
  expect(captainPreferenceFromApi("no")).toBe("Понизить");
  expect(parseProfileTab("captain")).toBe("captain");
});

test("captain tab is server seeded and saves through one atomic endpoint", async ({
  page,
}) => {
  await authenticateTestUser(page);
  const browserReads: string[] = [];
  const writes: string[] = [];

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      request.method() === "GET" &&
      [
        "/api/v1/profiles/me/deadlock/dream-slots",
        "/api/v1/content/game-assets",
      ].includes(url.pathname)
    ) {
      browserReads.push(`${request.method()} ${url.pathname}`);
    }
    if (
      request.method() === "PUT" &&
      url.pathname.startsWith("/api/v1/profiles/me")
    ) {
      writes.push(url.pathname);
    }
  });

  await page.goto("/profile/me?tab=captain");
  await expect(
    page.getByRole("button", { name: "Профиль капитана" })
  ).toHaveClass(/active/);
  await expect(page.getByTestId("profile-captain-team-name")).toHaveValue(
    "OldTeam"
  );

  await page.getByTestId("profile-captain-team-name").fill("Atomic Team");
  await page.getByTestId("profile-save-captain-button").click();
  await expect(page.getByTestId("profile-save-captain-button")).toContainText(
    "Сохранено"
  );

  expect(browserReads).toEqual([]);
  expect(writes).toEqual(["/api/v1/profiles/me/captain"]);
});

test("stored captain priority is visible after SSR and tab URL stays shareable", async ({
  page,
}) => {
  await authenticateTestUser(page, [
    { name: "captain-priority-yes-smoke", value: "1" },
  ]);

  await page.goto("/profile/me?tab=tournament");
  await expect(
    page.getByTestId("profile-pill-повысить")
  ).toHaveClass(/active/);

  await page.getByRole("button", { name: "Профиль капитана" }).click();
  await expect(page).toHaveURL(/[?&]tab=captain(?:&|$)/);
  await page.getByRole("button", { name: "Аккаунт" }).click();
  await expect(page).toHaveURL(/[?&]tab=account(?:&|$)/);
});

test("atomic captain editor preserves hero picker layout and save semantics", async ({
  page,
}) => {
  await authenticateTestUser(page);
  const requestBodies: CaptainPayload[] = [];

  await page.route("**/api/v1/profiles/me/captain", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.fallback();
      return;
    }
    const requestBody = route.request().postDataJSON() as CaptainPayload;
    requestBodies.push(requestBody);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        captain_team_name: requestBody.captain_team_name,
        dream_slots: requestBody.slots.map((slot) => ({
          user_id: "u_lisalexy",
          ...slot,
          updated_at: new Date().toISOString(),
        })),
      }),
    });
  });

  await page.goto("/profile/me?tab=captain");
  await expect(
    page.getByRole("heading", {
      name: "Автоматическое формирование команд для капитана",
    })
  ).toBeVisible();
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(0);

  const firstSlotCard = page.locator(".dream-slot-card").first();
  const firstSlotHeroes = firstSlotCard.locator(".dream-selected-heroes");
  const firstSlotBadge = firstSlotCard.locator(".state-badge");
  const firstSlotToggle = page.getByTestId(
    "profile-dream-slot-1-heroes-toggle"
  );
  const teamNameInput = page.getByTestId("profile-captain-team-name");
  const saveButton = page.getByTestId("profile-save-captain-button");
  const cancelButton = page.getByTestId("profile-cancel-captain-button");
  const carryButton = firstSlotCard.getByRole("button", {
    name: "Carry",
    exact: true,
  });
  const supportButton = firstSlotCard.getByRole("button", {
    name: "Support",
    exact: true,
  });

  await expect(firstSlotBadge).toHaveText("Не настроен");
  await expect(teamNameInput).toHaveValue("OldTeam");
  await expect(saveButton).toBeDisabled();
  await expect(cancelButton).toBeDisabled();

  await teamNameInput.fill("SmokeTeam");
  await carryButton.click();
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  await expect(saveButton).toContainText("Сохранено");
  await expect(firstSlotBadge).toHaveText("Настроен");
  expect(requestBodies).toHaveLength(1);
  const roleOnlyRequest = requestBodies[0]!;
  expect(roleOnlyRequest).toMatchObject({
    captain_team_name: "SmokeTeam",
  });
  expect(roleOnlyRequest.slots[0]).toMatchObject({
    slot_number: 1,
    allowed_roles: ["Carry"],
    desired_heroes: [],
  });

  await supportButton.click();
  await expect(cancelButton).toBeEnabled();
  await cancelButton.click();
  await expect(carryButton).toHaveClass(/active/);
  await expect(supportButton).not.toHaveClass(/active/);
  await expect(saveButton).toBeDisabled();

  const emptyHeroesBox = await firstSlotHeroes.boundingBox();
  expect(emptyHeroesBox?.height).toBeGreaterThanOrEqual(52);
  await expect(firstSlotHeroes).toContainText("Герои не выбраны.");

  await firstSlotToggle.click();
  const picker = page.getByTestId("profile-dream-slot-1-hero-picker");
  await expect(picker).toBeVisible();
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(1);
  const teammateGridBox = await page.locator(".teammate-grid").boundingBox();
  const pickerBox = await picker.boundingBox();
  expect(pickerBox?.width).toBeGreaterThanOrEqual(
    (teammateGridBox?.width ?? 0) - 2
  );

  const abrams = page.getByTestId("profile-dream-slot-1-hero-abrams");
  await abrams.click();
  await expect(abrams).toHaveClass(/selected/);
  await saveButton.click();
  await expect(saveButton).toContainText("Сохранено");
  await expect(page.locator(".dream-hero-picker-panel")).toHaveCount(0);
  expect(requestBodies).toHaveLength(2);
  const firstHeroRequest = requestBodies[1]!;
  expect(firstHeroRequest.slots[0]).toMatchObject({
    allowed_roles: ["Carry"],
    desired_heroes: ["Abrams"],
  });

  await firstSlotToggle.click();
  await page.getByTestId("profile-dream-slot-1-hero-apollo").click();
  await page.getByTestId("profile-dream-slot-1-hero-bebop").click();
  await page.getByTestId("profile-dream-slot-1-hero-billy").click();
  await page.getByTestId("profile-dream-slot-1-hero-calico").click();
  await expect(
    page.getByTestId("profile-dream-slot-1-hero-dynamo")
  ).toBeDisabled();

  await saveButton.click();
  await expect(saveButton).toContainText("Сохранено");
  await expect(saveButton).toBeDisabled();
  await expect(cancelButton).toBeDisabled();
  expect(requestBodies).toHaveLength(3);
  const finalRequest = requestBodies[2]!;
  expect(finalRequest.slots[0]).toMatchObject({
    slot_number: 1,
    allowed_roles: ["Carry"],
    desired_heroes: ["Abrams", "Apollo", "Bebop", "Billy", "Calico"],
  });

  if ((page.viewportSize()?.width ?? 0) <= 820) {
    const heroPositions = await firstSlotHeroes
      .locator(".dream-selected-hero")
      .evaluateAll((heroes) =>
        heroes.map((hero) => ({
          top: hero.getBoundingClientRect().top,
          width: hero.getBoundingClientRect().width,
        }))
      );
    expect(heroPositions).toHaveLength(5);
    expect(
      Math.max(...heroPositions.slice(0, 3).map(({ top }) => top)) -
        Math.min(...heroPositions.slice(0, 3).map(({ top }) => top))
    ).toBeLessThanOrEqual(1);
    expect(heroPositions[3].top).toBeGreaterThan(heroPositions[0].top);
    expect(
      new Set(heroPositions.slice(0, 3).map(({ width }) => Math.round(width)))
        .size
    ).toBe(1);
  }
});
