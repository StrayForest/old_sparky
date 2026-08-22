import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function authenticateTestUser(page: Page, extraCookies: Array<{ name: string; value: string }> = []) {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    },
    ...extraCookies.map((cookie) => ({ ...cookie, url: "http://127.0.0.1:3100" }))
  ]);
}

test("account email change uses shared Save, latest typed value, password confirmation, then email code", async ({ page }) => {
  const requestPayloads: Array<Record<string, unknown>> = [];

  await authenticateTestUser(page);
  await page.route("**/api/v1/auth/email-change/request", async (route) => {
    requestPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });
  await page.route("**/api/v1/auth/email-change/confirm", async (route) => {
    requestPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        email: "new-player@example.test",
        display_name: "lisalexy",
        status: "active",
        created_at: "2026-05-20T00:00:00Z",
        roles: ["authenticated_user", "player"],
        can_create_public_tournaments: false,
        has_password: true,
        steam_id: "76561198000000000",
        steam_linked: true
      })
    });
  });

  await page.goto("/profile/me?tab=account");

  const emailInput = page.getByTestId("profile-account-email");
  const profileSaveButton = page.getByTestId("profile-save-account-button");
  await expect(emailInput).toHaveValue("player@example.com");
  await expect(emailInput).toBeEditable();
  await expect(profileSaveButton).toBeDisabled();

  await emailInput.fill("");
  await emailInput.pressSequentially("new-player@example.test");
  await expect(profileSaveButton).toBeEnabled();
  await profileSaveButton.click();

  const passwordDialog = page.getByRole("dialog", { name: "Подтвердите смену почты" });
  await expect(passwordDialog).toBeVisible();
  await expect(passwordDialog).toHaveAttribute("id", "account-email-change-password-form");
  await expect(passwordDialog).toHaveAttribute("method", "post");
  await expect(passwordDialog).toHaveAttribute("autocomplete", "on");

  const username = passwordDialog.locator('input[name="username"]');
  const currentPassword = passwordDialog.locator('input[name="current_password"]');
  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(username).toHaveValue("player@example.com");
  await expect(currentPassword).toHaveAttribute("autocomplete", "current-password");
  await expect(currentPassword).toHaveAttribute("id", "email-change-current-password");

  // Password managers may update the native value without dispatching React
  // input/change events. Submission must use the actual DOM form value.
  await currentPassword.evaluate((input: HTMLInputElement) => {
    input.value = "CurrentPassword123!";
  });
  await passwordDialog.getByRole("button", { name: "Продолжить", exact: true }).click();

  await expect.poll(() => requestPayloads[0]).toEqual({
    email: "new-player@example.test",
    current_password: "CurrentPassword123!"
  });
  await expect(page.getByRole("dialog", { name: "Подтвердите новую почту" })).toBeVisible();
  await expect(page.getByText("Код отправлен на new-player@example.test")).toBeVisible();

  await page.getByLabel("Код подтверждения").fill("654321");
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();

  await expect.poll(() => requestPayloads[1]).toEqual({
    email: "new-player@example.test",
    code: "654321"
  });
  await expect(emailInput).toHaveValue("new-player@example.test");
  await expect(profileSaveButton).toBeDisabled();
});

test("Steam-only account links optional email through shared profile Save", async ({ page }) => {
  const requestPayloads: Array<Record<string, unknown>> = [];

  await authenticateTestUser(page, [{ name: "steam-only-smoke", value: "1" }]);
  await page.route("**/api/v1/auth/email-link/request", async (route) => {
    requestPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });
  await page.route("**/api/v1/auth/email-link/confirm", async (route) => {
    requestPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_steam_only",
        email: "linked@example.test",
        display_name: "SteamPlayer",
        status: "active",
        created_at: "2026-05-20T00:00:00Z",
        roles: ["authenticated_user", "player"],
        can_create_public_tournaments: false,
        has_password: false,
        steam_id: "76561198999999999",
        steam_linked: true
      })
    });
  });

  await page.goto("/profile/me?tab=account");

  const emailInput = page.getByTestId("profile-account-email");
  const profileSaveButton = page.getByTestId("profile-save-account-button");
  await expect(emailInput).toHaveValue("");
  await expect(profileSaveButton).toBeDisabled();

  await emailInput.fill("linked@example.test");
  await expect(profileSaveButton).toBeEnabled();
  await profileSaveButton.click();

  await expect(page.getByRole("dialog", { name: "Подтвердите смену почты" })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "Подтвердите новую почту" })).toBeVisible();
  await expect.poll(() => requestPayloads[0]).toEqual({ email: "linked@example.test" });

  await page.getByLabel("Код подтверждения").fill("654321");
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();

  await expect.poll(() => requestPayloads[1]).toEqual({
    email: "linked@example.test",
    code: "654321"
  });
  await expect(emailInput).toHaveValue("linked@example.test");
  await expect(profileSaveButton).toBeDisabled();
});

test("invalid account email is rejected by shared Save before an API request", async ({ page }) => {
  let requestCount = 0;

  await authenticateTestUser(page);
  await page.route("**/api/v1/auth/email-change/request", async (route) => {
    requestCount += 1;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 })
    });
  });

  await page.goto("/profile/me?tab=account");

  const emailInput = page.getByTestId("profile-account-email");
  const profileSaveButton = page.getByTestId("profile-save-account-button");
  await emailInput.fill("broken-address@");
  await emailInput.blur();
  await expect(profileSaveButton).toHaveAttribute("aria-disabled", "false");
  await profileSaveButton.click();

  await expect(page.getByText("Введите корректную почту.")).toBeVisible();
  await expect(emailInput).toHaveAttribute("aria-invalid", "true");
  expect(requestCount).toBe(0);
});
