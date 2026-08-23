import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

function source(relativePath: string): string {
  return readFileSync(resolve(process.cwd(), relativePath), "utf8");
}

test("invite-only server pages convert anonymous workspace access into login flow", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const bracketPage = source("app/(site)/tournaments/[slug]/bracket/page.tsx");

  for (const page of [detailPage, bracketPage]) {
    expect(page).toContain("PlatformApiError");
    expect(page).toContain("error.status === 401");
    expect(page).toContain("/auth/login?returnTo=");
  }
});

test("private registration is gated by the backend invite capability", () => {
  const api = source("lib/platform-api.ts");
  const types = source("lib/types.ts");
  const actions = source("components/tournaments/tournament-registration-actions.tsx");

  expect(api).toContain("current_user_has_invite_access?: boolean | null");
  expect(api).toContain("currentUserHasInviteAccess: Boolean(item.current_user_has_invite_access)");
  expect(types).toContain("currentUserHasInviteAccess?: boolean");
  expect(actions).toContain("const hasRegistrationAccess = Boolean(");
  expect(actions).toContain("tournament.currentUserHasInviteAccess");
  expect(actions).toContain("&& hasRegistrationAccess");
});

test("auth and Steam capabilities fail closed without runtime security config", () => {
  const securityConfig = source("components/auth/use-auth-security-config.ts");
  const steamIdentity = source("components/profile/account-identities.tsx");

  expect(securityConfig).toContain("public_registration_enabled: false");
  expect(securityConfig).toContain("email_verification_required: true");
  expect(securityConfig).toContain("steam_login_enabled: false");
  expect(steamIdentity).toContain("useAuthSecurityConfig");
  expect(steamIdentity).toContain("security.status === \"ready\"");
  expect(steamIdentity).toContain("security.config?.steam_login_enabled === true");
});

test("tournament creation serializes submit and invite-code async state", () => {
  const createForm = source("components/tournaments/create-tournament-form.tsx");

  expect(createForm).toContain("const inviteRequestGenerationRef = useRef(0)");
  expect(createForm).toContain("const submitInFlightRef = useRef(false)");
  expect(createForm).toContain("if (submitInFlightRef.current || createdTournamentSlug)");
  expect(createForm).toContain("disabled={status === \"saving\" ||");
  expect(createForm).toContain("values.inviteCode.length >= 10");
  expect(createForm).not.toContain("values.inviteCode.length >= 6");
  expect(createForm).toContain("Пн Вт Ср Чт Пт Сб Вс");
});

test("profile editor mutations cannot overlap their editable drafts", () => {
  const account = source("components/profile/editor/account-profile-tab.tsx");
  const tournament = source("components/profile/editor/tournament-profile-tab.tsx");
  const captain = source("components/profile/editor/captain-profile-tab.tsx");

  expect(account).toContain("const accountSaveInFlightRef = useRef(false)");
  expect(account).toContain("if (accountSaveInFlightRef.current)");
  expect(account).toContain("disabled={saveState === \"saving\"}");

  for (const editor of [tournament, captain]) {
    expect(editor).toContain("const saveInFlightRef = useRef(false)");
    expect(editor).toContain("if (saveInFlightRef.current)");
    expect(editor).toContain("disabled={saveState === \"saving\"}");
  }
});

test("admin cleanup keeps committed success independent from reload", () => {
  const admin = source("components/admin/admin-console.tsx");
  const cleanupFailure = admin.indexOf("setError(platformApiMessage(requestError, t(\"admin.preprodCleanupFailed\")))");
  const reload = admin.indexOf("await onReload();", cleanupFailure);
  const committedComment = admin.indexOf("Cleanup is already committed", cleanupFailure);

  expect(cleanupFailure).toBeGreaterThan(-1);
  expect(reload).toBeGreaterThan(cleanupFailure);
  expect(committedComment).toBeGreaterThan(reload);
});
