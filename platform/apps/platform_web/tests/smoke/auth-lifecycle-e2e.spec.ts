import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { lstatSync, readFileSync } from "node:fs";

const API_ORIGIN = "http://127.0.0.1:3199";
const WEB_ORIGIN = "http://127.0.0.1:3100";
const FIXTURE_EMAIL = "auth-e2e@example.com";
const REGISTRATION_PASSWORD = "AuthE2E-Registration-Password-1!";
const RESET_PASSWORD = "AuthE2E-Reset-Password-2!";
const DISPLAY_NAME = "AuthE2E";

type ControlState = {
  siteverify_calls: number;
  accepted: number;
  rejected: number;
  wrong_action: number;
  wrong_hostname: number;
  missing_action: number;
  provider_error: number;
};

type CleanupResult = {
  cleaned: boolean;
  audit_actions: string[];
  sessions: number;
};

declare global {
  interface Window {
    __authE2eWidget?: {
      action: string;
      callback: (token: string) => void;
    };
    __authE2eDeliver?: (token: string) => void;
  }
}

let controlToken: string | null = null;
let cleanupComplete = false;

function getControlToken(): string {
  if (controlToken) {
    return controlToken;
  }
  const file = process.env.PLATFORM_AUTH_E2E_CONTROL_FILE;
  if (!file) {
    throw new Error("Auth E2E control file is unavailable.");
  }
  const metadata = lstatSync(file);
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
    throw new Error("Auth E2E control file metadata is unsafe.");
  }
  const token = readFileSync(file, "ascii").trim();
  if (!/^[A-Za-z0-9_-]{40,60}$/u.test(token)) {
    throw new Error("Auth E2E control token format is invalid.");
  }
  controlToken = token;
  return token;
}

async function controlRequest<T>(path: string, method = "GET"): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_ORIGIN}/__auth_e2e__/${path}`, {
      method,
      headers: { "x-auth-e2e-control": getControlToken() }
    });
  } catch {
    throw new Error("Auth E2E control request failed.");
  }
  if (!response.ok) {
    throw new Error("Auth E2E control request failed.");
  }
  try {
    return await response.json() as T;
  } catch {
    throw new Error("Auth E2E control response is invalid.");
  }
}

async function getCode(purpose: "verification" | "reset"): Promise<string> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const payload = await controlRequest<{ code: string }>(`mail/${purpose}`);
      if (/^[0-9]{6}$/u.test(payload.code)) {
        return payload.code;
      }
    } catch {
      // SMTP delivery is an in-process background task; poll only the
      // in-memory test sink for this exact fixture and purpose.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("Auth E2E mail code is unavailable.");
}

async function stats(): Promise<ControlState> {
  return controlRequest<ControlState>("stats");
}

async function cleanup(): Promise<CleanupResult> {
  const result = await controlRequest<CleanupResult>("cleanup", "POST");
  cleanupComplete = result.cleaned === true;
  return result;
}

async function installWidgetStub(page: Page): Promise<void> {
  await page.route("https://challenges.cloudflare.com/turnstile/v0/api.js*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `(() => {
        let nextId = 0;
        const widgets = new Map();
        window.turnstile = {
          render(container, options) {
            const id = String(++nextId);
            widgets.set(id, options);
            window.__authE2eWidget = { action: options.action, callback: options.callback };
            window.__authE2eDeliver = (token) => window.__authE2eWidget?.callback(token);
            container.dataset.turnstileAction = options.action;
            options.callback && options.appearance === "never" && options.callback("");
            return id;
          },
          remove(id) { widgets.delete(id); },
          reset(id) { if (!widgets.has(id)) return; },
        };
      })();`
    });
  });
}

async function deliver(page: Page, kind: string, action: string): Promise<void> {
  await expect(page.locator(".auth-turnstile")).toBeVisible();
  await page.evaluate(({ kind, action }) => {
    const token = `auth-e2e:${kind}:${action}`;
    window.__authE2eDeliver?.(token);
  }, { kind, action });
}

async function expectSessionStatus(context: BrowserContext, expectedStatus: number): Promise<void> {
  const response = await context.request.get(`${API_ORIGIN}/api/v1/auth/session`);
  expect(response.status()).toBe(expectedStatus);
}

async function profileLogout(page: Page, context: BrowserContext): Promise<void> {
  await page.goto("/profile/me?tab=account");
  await page.getByTestId("profile-logout-button").click();
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expectSessionStatus(context, 401);
}

async function loginThroughUi(page: Page, password: string): Promise<void> {
  await page.context().clearCookies();
  await page.goto("/auth/login?returnTo=%2Fprofile%2Fme");
  const form = page.locator("form#login-form");
  await form.locator('input[name="username"]').fill(FIXTURE_EMAIL);
  await form.locator('input[name="current_password"]').fill(password);
  await form.locator('button[type="submit"]').click();
  await expect(page.locator(".auth-turnstile")).toBeVisible();
  await deliver(page, "valid", "login");
  await expect(page).toHaveURL(/\/profile(?:\/me)?(?:\?|$)/u);
  await expectSessionStatus(page.context(), 200);
}

async function waitForCounter(key: keyof ControlState, minimum: number): Promise<void> {
  await expect.poll(async () => (await stats())[key]).toBeGreaterThanOrEqual(minimum);
}

test("auth lifecycle uses the real API, local mail capture, and strict verification", async ({
  browser,
  page
}) => {
  const contexts: { extra: BrowserContext | null; reset: BrowserContext | null } = {
    extra: null,
    reset: null
  };
  let cleanupResult: CleanupResult | null = null;
  try {
    await test.step("01-registration-verification-code", async () => {
      await installWidgetStub(page);
      await page.goto("/auth/register");
      const registration = page.locator("form#registration-form");
      await registration.locator('input[name="display_name"]').fill(DISPLAY_NAME);
      await registration.locator('input[name="username"]').fill(FIXTURE_EMAIL);
      await registration.locator('input[name="new_password"]').fill(REGISTRATION_PASSWORD);
      await registration.locator('button[type="submit"]').click();

      await expect(page.locator(".auth-turnstile")).toBeVisible();
      await deliver(page, "provider-error", "register");
      await waitForCounter("provider_error", 1);
      await expect(page.locator(".auth-error[role='alert']")).toBeVisible();
      await deliver(page, "wrong-hostname", "register");
      await registration.locator('button[type="submit"]').click();
      await waitForCounter("wrong_hostname", 1);
      await deliver(page, "rejected", "register");
      await waitForCounter("rejected", 1);
      await deliver(page, "valid", "register");
      await registration.locator('button[type="submit"]').click();
      await expect(page.locator("form#registration-code-form")).toBeVisible();

      const registrationCode = await getCode("verification");
      const wrongRegistrationCode = registrationCode === "000000" ? "000001" : "000000";
      const registrationCodeForm = page.locator("form#registration-code-form");
      await registrationCodeForm.locator('input[name="one_time_code"]').fill(wrongRegistrationCode);
      await registrationCodeForm.locator('button[type="submit"]').click();
      await expect(page.locator(".auth-error[role='alert']")).toBeVisible();
      await registrationCodeForm.locator('input[name="one_time_code"]').fill(registrationCode);
      await registrationCodeForm.locator('button[type="submit"]').click();
      await expect(page).toHaveURL(/\/profile(?:\/me)?(?:\?|$)/u);
      await expectSessionStatus(page.context(), 200);
    });

    await test.step("02-login-and-second-session", async () => {
      await profileLogout(page, page.context());
      await page.context().clearCookies();
      await loginThroughUi(page, REGISTRATION_PASSWORD);

      contexts.extra = await browser.newContext({ baseURL: WEB_ORIGIN });
      const secondPage = await contexts.extra.newPage();
      await installWidgetStub(secondPage);
      await loginThroughUi(secondPage, REGISTRATION_PASSWORD);
      await expectSessionStatus(contexts.extra, 200);
    });

    await test.step("03-password-reset-and-session-invalidation", async () => {
      contexts.reset = await browser.newContext({ baseURL: WEB_ORIGIN });
      const resetPage = await contexts.reset.newPage();
      await installWidgetStub(resetPage);
      await resetPage.goto("/reset-password");
      const resetRequestForm = resetPage.locator("form#password-reset-request-form");
      await resetRequestForm.locator('input[name="email"]').fill(FIXTURE_EMAIL);
      await resetRequestForm.locator('button[type="submit"]').click();
      await expect(resetPage.locator(".auth-turnstile")).toBeVisible();
      await deliver(resetPage, "wrong-action", "reset_request");
      await resetRequestForm.locator('button[type="submit"]').click();
      await waitForCounter("wrong_action", 1);
      await expect(resetPage.locator(".auth-error[role='alert']")).toBeVisible();
      await deliver(resetPage, "missing-action", "reset_request");
      await resetRequestForm.locator('button[type="submit"]').click();
      await waitForCounter("missing_action", 1);
      await deliver(resetPage, "rejected", "reset_request");
      await resetRequestForm.locator('button[type="submit"]').click();
      await waitForCounter("rejected", 2);
      await deliver(resetPage, "valid", "reset_request");
      await resetRequestForm.locator('button[type="submit"]').click();

      const resetCode = await getCode("reset");
      const wrongResetCode = resetCode === "000000" ? "000001" : "000000";
      const resetCodeForm = resetPage.locator("form#password-reset-code-form");
      await resetCodeForm.locator('input[name="code"]').fill(wrongResetCode);
      await resetCodeForm.locator('button[type="submit"]').click();
      await expect(resetPage.locator(".auth-error[role='alert']")).toBeVisible();
      await resetCodeForm.locator('input[name="code"]').fill(resetCode);
      await resetCodeForm.locator('button[type="submit"]').click();

      const resetPasswordForm = resetPage.locator("form#password-reset-password-form");
      await resetPasswordForm.locator('input[name="username"]').fill(FIXTURE_EMAIL);
      await resetPasswordForm.locator('input[name="new_password"]').fill(RESET_PASSWORD);
      await resetPasswordForm.locator('input[name="confirm_password"]').fill(RESET_PASSWORD);
      await resetPasswordForm.locator('button[type="submit"]').click();
      await expect(resetPage).toHaveURL(/\/profile(?:\/me)?(?:\?|$)/u);
      await expectSessionStatus(resetPage.context(), 200);
      await expectSessionStatus(page.context(), 401);
      if (!contexts.extra) throw new Error("Auth E2E second login context is unavailable.");
      await expectSessionStatus(contexts.extra, 401);
    });

    await test.step("04-change-password-and-relogin", async () => {
      if (!contexts.reset) throw new Error("Auth E2E reset context is unavailable.");
      const resetPage = contexts.reset.pages()[0];
      await resetPage.goto("/profile/me?tab=account");
      const accountPasswordForm = resetPage.locator("form#account-password-change-form");
      await accountPasswordForm.locator('input[name="current_password"]').fill(RESET_PASSWORD);
      await accountPasswordForm.locator('input[name="new_password"]').fill(REGISTRATION_PASSWORD);
      await accountPasswordForm.locator('input[name="confirm_password"]').fill(REGISTRATION_PASSWORD);
      await resetPage.getByTestId("profile-save-security-button").click();
      await expect(resetPage.getByTestId("profile-password-change-success")).toBeVisible();
      await expectSessionStatus(resetPage.context(), 200);
      await profileLogout(resetPage, resetPage.context());
      await resetPage.context().clearCookies();
      await loginThroughUi(resetPage, REGISTRATION_PASSWORD);
      await profileLogout(resetPage, resetPage.context());

      const finalStats = await stats();
      expect(finalStats.provider_error).toBeGreaterThanOrEqual(1);
      expect(finalStats.wrong_hostname).toBeGreaterThanOrEqual(1);
      expect(finalStats.wrong_action).toBeGreaterThanOrEqual(1);
      expect(finalStats.missing_action).toBeGreaterThanOrEqual(1);
      expect(finalStats.accepted).toBeGreaterThanOrEqual(3);
    });

    await test.step("05-exact-fixture-cleanup", async () => {
      cleanupResult = await cleanup();
      expect(cleanupResult.cleaned).toBe(true);
      expect(cleanupResult.sessions).toBe(6);
      expect(cleanupResult.audit_actions).toEqual([
        "auth.register",
        "auth.email_verification.confirm",
        "auth.logout",
        "auth.login",
        "auth.login",
        "auth.password_reset.request",
        "auth.password_reset.confirm",
        "auth.account.update",
        "auth.logout",
        "auth.login",
        "auth.logout"
      ]);
    });
  } finally {
    if (!cleanupComplete) {
      try {
        cleanupResult = await cleanup();
      } catch {
        // The job's disposable database is destroyed on every exit path.
      }
    }
    await contexts.extra?.close();
    await contexts.reset?.close();
  }
});

test.afterAll(async () => {
  if (!cleanupComplete) {
    try {
      await cleanup();
    } catch {
      // The job's disposable database is destroyed on every exit path.
    }
  }
});
