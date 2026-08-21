import { expect, test } from "@playwright/test";

const authUser = {
  id: "u_password_manager",
  email: "autofill@example.test",
  display_name: "autofill-user",
  status: "active",
  created_at: "2026-08-18T00:00:00Z",
  roles: [],
  can_create_public_tournaments: false,
  avatar_url: null,
  avatar_media: null,
  steam_id: null,
  steam_linked: false,
  has_password: true,
};

async function mockAuthInfrastructure(page: import("@playwright/test").Page) {
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
}

test("registration accepts a generated password inserted without React events", async ({ page }) => {
  await mockAuthInfrastructure(page);

  let submittedPayload: Record<string, unknown> | null = null;
  let releaseRegistration!: () => void;
  const registrationGate = new Promise<void>((resolve) => {
    releaseRegistration = resolve;
  });

  await page.route("**/api/v1/auth/register", async (route) => {
    submittedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await registrationGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ user: authUser, expires_at: "2026-08-19T00:00:00Z" }),
    });
  });

  await page.goto("/auth/register");

  const form = page.locator("form#registration-form");
  await expect(form).toHaveAttribute("name", "registration-form");
  await expect(form).toHaveAttribute("method", "post");
  await expect(form).toHaveAttribute("autocomplete", "on");

  const displayName = form.locator('input[name="display_name"]');
  const username = form.locator('input[name="username"]');
  const password = form.locator('input[name="new_password"]');

  await expect(username).toHaveAttribute("id", "register-username");
  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(username).toHaveAttribute("type", "email");
  await expect(password).toHaveAttribute("id", "new-password");
  await expect(password).toHaveAttribute("autocomplete", "new-password");
  await expect(password).toHaveAttribute("type", "password");

  await displayName.fill("Autofill User");
  await username.evaluate((input: HTMLInputElement) => {
    input.value = "autofill@example.test";
  });
  await password.evaluate((input: HTMLInputElement) => {
    input.value = "GeneratedRegistrationPassword123!";
  });

  await expect(username).toHaveValue("autofill@example.test");
  await expect(password).toHaveValue("GeneratedRegistrationPassword123!");
  await form.locator('button[type="submit"]').click();

  await expect.poll(() => submittedPayload).toEqual({
    display_name: "Autofill User",
    email: "autofill@example.test",
    password: "GeneratedRegistrationPassword123!",
  });

  // Keep credential inputs available to the browser while the async
  // registration request is pending. Disabling them can break password
  // manager association with the username/new-password pair.
  await expect(username).toBeEnabled();
  await expect(password).toBeEnabled();
  await expect(username).toHaveValue("autofill@example.test");
  await expect(password).toHaveValue("GeneratedRegistrationPassword123!");

  releaseRegistration();
});

test("login exposes a native current-password credential form", async ({ page }) => {
  await mockAuthInfrastructure(page);

  let submittedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/v1/auth/login", async (route) => {
    submittedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ user: authUser, expires_at: "2026-08-19T00:00:00Z" }),
    });
  });

  await page.goto("/auth/login");

  const form = page.locator("form#login-form");
  const username = form.locator('input[name="username"]');
  const password = form.locator('input[name="current_password"]');

  await expect(form).toHaveAttribute("name", "login-form");
  await expect(form).toHaveAttribute("method", "post");
  await expect(username).toHaveAttribute("id", "login-username");
  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(password).toHaveAttribute("id", "current-password");
  await expect(password).toHaveAttribute("autocomplete", "current-password");

  await username.evaluate((input: HTMLInputElement) => {
    input.value = "autofill@example.test";
  });
  await password.evaluate((input: HTMLInputElement) => {
    input.value = "StoredPassword123!";
  });
  await form.locator('button[type="submit"]').click();

  await expect.poll(() => submittedPayload).toEqual({
    email: "autofill@example.test",
    password: "StoredPassword123!",
  });
});
