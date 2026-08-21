import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function authenticateTestUser(page: Page) {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100",
    },
  ]);
}

test("password change keeps all credential fields in one native form", async ({
  page,
}) => {
  await authenticateTestUser(page);

  let submittedPayload: Record<string, unknown> | null = null;

  await page.route("**/api/v1/auth/csrf", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ csrf_token: "c".repeat(64) }),
    });
  });

  await page.route("**/api/v1/auth/account", async (route) => {
    if (route.request().method() !== "PATCH") {
      await route.fallback();
      return;
    }
    submittedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_lisalexy",
        email: "lisalexy@example.test",
        display_name: "lisalexy",
        status: "active",
        created_at: "2026-08-18T00:00:00Z",
        roles: [],
        can_create_public_tournaments: false,
        avatar_url: null,
        avatar_media: null,
        steam_id: null,
        steam_linked: false,
        has_password: true,
      }),
    });
  });

  await page.goto("/profile/me?tab=account");

  const username = page.locator('input[name="username"]');
  const currentPassword = page.locator('input[name="current_password"]');
  const newPassword = page.locator('input[name="new_password"]');
  const confirmPassword = page.locator('input[name="confirm_password"]');
  const form = newPassword.locator("xpath=ancestor::form[1]");

  await expect(form).toHaveAttribute("id", "account-password-change-form");
  await expect(form).toHaveAttribute("method", "post");
  await expect(form).toHaveAttribute("autocomplete", "on");
  await expect(form.getByText("Действующий пароль", { exact: true })).toHaveCount(0);
  await expect(form.getByText("Текущий пароль", { exact: true })).toBeVisible();

  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(username).toHaveAttribute("id", "account-username");
  await expect(username).not.toHaveAttribute("readonly", "");

  await expect(currentPassword).toHaveAttribute("autocomplete", "current-password");
  await expect(currentPassword).toHaveAttribute("id", "current-password");
  await expect(currentPassword).not.toHaveAttribute("minlength", "10");
  await expect(newPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(newPassword).toHaveAttribute("id", "new-password");
  await expect(newPassword).toHaveAttribute("minlength", "10");
  await expect(confirmPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(confirmPassword).toHaveAttribute("id", "confirm-password");

  const sameForm = await currentPassword.evaluate((currentInput) => {
    const currentForm = currentInput.closest("form");
    const newInput = document.querySelector('input[name="new_password"]');
    const confirmInput = document.querySelector('input[name="confirm_password"]');
    const usernameInput = document.querySelector('input[name="username"]');
    return Boolean(
      currentForm &&
        newInput?.closest("form") === currentForm &&
        confirmInput?.closest("form") === currentForm &&
        usernameInput?.closest("form") === currentForm
    );
  });
  expect(sameForm).toBe(true);

  // Reproduce Android/password-manager style DOM writes without React events.
  await currentPassword.evaluate((input: HTMLInputElement) => {
    input.value = "CurrentPassword123!";
  });
  await newPassword.evaluate((input: HTMLInputElement) => {
    input.value = "FreshPassword123!";
  });
  await confirmPassword.evaluate((input: HTMLInputElement) => {
    input.value = "FreshPassword123!";
  });

  await expect(currentPassword).toHaveValue("CurrentPassword123!");
  await expect(newPassword).toHaveValue("FreshPassword123!");
  await expect(confirmPassword).toHaveValue("FreshPassword123!");
  await expect(page.getByTestId("profile-save-security-button")).toBeEnabled();
  await page.getByTestId("profile-save-security-button").click();

  const success = page.getByTestId("profile-password-change-success");
  await expect(success).toBeVisible();
  await expect(success).toContainText("Пароль изменён.");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  expect(submittedPayload).toEqual({
    current_password: "CurrentPassword123!",
    email: null,
    new_password: "FreshPassword123!",
  });

  await expect(currentPassword).toHaveValue("");
  await expect(newPassword).toHaveValue("");
  await expect(confirmPassword).toHaveValue("");
});
