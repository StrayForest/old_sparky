import { expect, test } from "@playwright/test";

test("password recovery exposes a native password-manager form and accepts silent autofill", async ({
  page,
}) => {
  let submittedPassword: string | null = null;

  await page.route("**/api/v1/auth/session", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Not authenticated" }),
    });
  });

  await page.route("**/api/v1/auth/security-config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        public_registration_enabled: true,
        email_verification_required: false,
        turnstile_mode: "off",
        turnstile_site_key: null,
        steam_login_enabled: false,
      }),
    });
  });

  await page.route("**/api/v1/auth/csrf", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ csrf_token: "c".repeat(64) }),
    });
  });

  await page.route("**/api/v1/auth/password-reset/request", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true, retry_after_seconds: 60 }),
    });
  });

  await page.route("**/api/v1/auth/password-reset/verify-code", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ accepted: true }),
    });
  });

  await page.route("**/api/v1/auth/password-reset/confirm", async (route) => {
    const payload = route.request().postDataJSON() as {
      new_password?: string;
    };
    submittedPassword = payload.new_password ?? null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: "u_reset",
          email: "reset@example.test",
          display_name: "reset-user",
          status: "active",
          created_at: "2026-08-18T00:00:00Z",
          roles: [],
          can_create_public_tournaments: false,
          avatar_url: null,
          avatar_media: null,
          steam_id: null,
          steam_linked: false,
          has_password: true,
        },
        expires_at: "2026-08-19T00:00:00Z",
      }),
    });
  });

  await page.goto("/reset-password");

  let form = page.locator("form.auth-form");
  let submit = form.locator('button[type="submit"]');

  await expect(form).toHaveAttribute("id", "password-reset-request-form");
  const requestEmail = form.locator('input[name="email"]');
  await expect(requestEmail).toHaveAttribute("autocomplete", "email");
  await requestEmail.fill("reset@example.test");
  await expect(submit).toBeEnabled();
  await submit.click();

  form = page.locator("form.auth-form");
  submit = form.locator('button[type="submit"]');
  await expect(form).toHaveAttribute("id", "password-reset-code-form");
  await expect(form.locator('input[name="code"]')).toHaveAttribute(
    "id",
    "password-reset-code"
  );
  await form.locator('input[name="code"]').fill("123456");
  await submit.click();

  form = page.locator("form.auth-form");
  submit = form.locator('button[type="submit"]');
  await expect(form).toHaveAttribute("id", "password-reset-password-form");
  await expect(form).toHaveAttribute("name", "password-reset-password-form");
  await expect(form).toHaveAttribute("method", "post");

  const username = form.locator('input[name="username"]');
  const newPassword = form.locator('input[name="new_password"]');
  const confirmPassword = form.locator('input[name="confirm_password"]');

  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(username).toHaveAttribute("id", "username");
  await expect(username).toHaveAttribute("type", "email");
  await expect(username).not.toHaveAttribute("readonly", "");
  await expect(username).toHaveValue("reset@example.test");

  await expect(newPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(newPassword).toHaveAttribute("id", "new-password");
  await expect(confirmPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(confirmPassword).toHaveAttribute("id", "confirm-password");
  await expect(newPassword).toBeEnabled();
  await expect(confirmPassword).toBeEnabled();

  // Reproduce password managers that update native input.value without
  // dispatching React input/change events. Submission must use DOM values.
  await newPassword.evaluate((input: HTMLInputElement) => {
    input.value = "GeneratedRecoveryPassword123!";
  });
  await confirmPassword.evaluate((input: HTMLInputElement) => {
    input.value = "GeneratedRecoveryPassword123!";
  });

  await expect(newPassword).toHaveValue("GeneratedRecoveryPassword123!");
  await expect(confirmPassword).toHaveValue("GeneratedRecoveryPassword123!");
  await submit.click();

  await expect.poll(() => submittedPassword).toBe(
    "GeneratedRecoveryPassword123!"
  );
});
