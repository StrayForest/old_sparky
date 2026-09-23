import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

function source(relativePath: string): string {
  return readFileSync(resolve(process.cwd(), relativePath), "utf8");
}

test("invite-only pages convert missing workspace proof into invite-code flow", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const detailClientPage = source("components/tournaments/tournament-detail-client-page.tsx");
  const bracketPage = source("app/(site)/tournaments/[slug]/bracket/page.tsx");
  const bracketBoard = source("components/bracket/bracket-board.tsx");

  expect(detailPage).toContain("TournamentDetailClientPage");
  expect(detailPage).toContain("initialTournament={initialTournament}");
  expect(detailClientPage).toContain("PlatformApiError");
  expect(detailClientPage).toContain("initialTournament?: TournamentDetail");
  expect(detailClientPage).toContain("initialRequestRef");
  expect(detailClientPage).toContain("serverSeedVersionRef");
  expect(detailClientPage).toContain("serverSeedRef.current.payload !== initialTournament");
  expect(detailClientPage).toContain("initialRequest.payload === initialTournament");
  expect(detailClientPage).toContain("key={serverSeedVersion}");
  expect(detailClientPage).toContain("retryGeneration === 0");
  expect(detailClientPage).toContain("error.status === 401");
  expect(detailClientPage).toContain("TournamentInviteGate");
  expect(bracketPage).toContain("PlatformApiError");
  expect(bracketPage).toContain("error.status === 401");
  expect(bracketPage).toContain("invite_code");
  expect(bracketPage).toContain("normalizeTournamentInviteCode");
  expect(bracketBoard).toContain("inviteCode,");
  expect(bracketBoard).toContain("getTournamentBracket(slug, {}, {");
});

test("private registration is gated by the invite code carried by the room URL", () => {
  const api = source("lib/platform-api.ts");
  const actions = source("components/tournaments/tournament-registration-actions.tsx");

  expect(api).toContain("invite_code?: string | null");
  expect(api).toContain("inviteCode: item.invite_code ?? null");
  expect(api).toContain("normalizeTournamentInviteCode(options.inviteCode)");
  expect(api).toContain('params.set("invite_code", inviteCode)');
  expect(api).toContain('if (typeof value !== "string")');
  expect(api).not.toMatch(/console\.(?:debug|info|log|warn|error).*inviteCode/u);
  expect(actions).toContain("const hasRegistrationAccess = Boolean(");
  expect(actions).toContain("tournament.inviteCode");
  expect(actions).toContain("&& hasRegistrationAccess");
  expect(actions).toContain("data-testid=\"tournament-read-only-workflow\"");
});

test("registration actions ignore stale tournament and session responses", () => {
  const actions = source("components/tournaments/tournament-registration-actions.tsx");
  const detailClient = source("components/tournaments/tournament-detail-client-page.tsx");

  expect(actions).toContain("const actionGeneration = useRef(0)");
  expect(actions).toContain("const activeActionController = useRef<AbortController | null>(null)");
  expect(actions).toContain("const requestSlug = tournament.slug");
  expect(actions).toContain("requestIsCurrent(requestGeneration, requestIdentity, controller)");
  expect(detailClient).toContain("const requestSessionIdentity = sessionIdentity");
  expect(detailClient).toContain("requestSlug !== slug");
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

test("my profile uses the protected bootstrap request without a page auth probe", () => {
  const page = source("app/(site)/profile/me/page.tsx");

  expect(page).toContain("getServerProfileBootstrap");
  expect(page).toContain("getServerProfileHeroNames");
  expect(page).toContain("Promise.all");
  expect(page).not.toContain("getServerCurrentUser");
});

test("operations access fails closed and never trusts an API endpoint", () => {
  const access = source("lib/platform-ops-access.ts");

  expect(access).not.toContain("127.0.0.1:9");
  expect(access).not.toContain("smokeFallback");
  expect(access).toContain('role === "admin"');
  expect(access).toContain('role === "superadmin"');
  expect(access).toContain("return Boolean(hasAdminRole)");
});

test("notFound-capable segments keep an intentional no-segment-loading contract", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const bracketPage = source("app/(site)/tournaments/[slug]/bracket/page.tsx");
  const profilePage = source("app/(site)/tournaments/[slug]/profiles/[userId]/page.tsx");
  const operationsPage = source("app/platform-ops/page.tsx");
  const detailLoading = resolve("app/(site)/tournaments/[slug]/loading.tsx");
  const bracketLoading = resolve("app/(site)/tournaments/[slug]/bracket/loading.tsx");
  const profileLoading = resolve("app/(site)/tournaments/[slug]/profiles/[userId]/loading.tsx");
  const operationsLoading = resolve("app/platform-ops/loading.tsx");
  const listLoading = resolve("app/(site)/tournaments/(list)/loading.tsx");
  const detailClientPage = source("components/tournaments/tournament-detail-client-page.tsx");

  expect(detailPage).toContain("Intentional no-segment-loading contract");
  expect(bracketPage).toContain("if (!workspace)");
  expect(bracketPage).toContain("notFound()");
  expect(profilePage).toContain("notFound()");
  expect(operationsPage).toContain("Intentional no-segment-loading contract");
  expect(existsSync(detailLoading)).toBe(false);
  expect(existsSync(bracketLoading)).toBe(false);
  expect(existsSync(profileLoading)).toBe(false);
  expect(existsSync(operationsLoading)).toBe(false);
  expect(existsSync(listLoading)).toBe(true);
  expect(detailClientPage).toContain('RouteLoadingShell variant="tournament-detail"');
});

test("tournament detail resolves missing slugs before committing the client shell", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const workspaceAccess = source("../platform_api/app/services/tournament_workspace_access.py");

  expect(detailPage).toContain("getTournamentWorkspace");
  expect(detailPage).toContain("const cookieHeader = (await cookies()).toString()");
  expect(detailPage).toContain("if (!workspace)");
  expect(detailPage).toContain("notFound()");
  expect(detailPage).toContain("error.status === 401 || error.status === 403");
  expect(detailPage).toContain("TournamentDetailClientPage");
  expect(workspaceAccess).toContain('status_code=status.HTTP_404_NOT_FOUND');
  expect(workspaceAccess).toContain('detail="Tournament not found."');
  expect(workspaceAccess).toContain('status_code=status.HTTP_401_UNAUTHORIZED');
  expect(workspaceAccess).toContain('status_code=status.HTTP_403_FORBIDDEN');
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
  const admin = source("components/admin/admin-preprod-page.tsx");
  const cleanupFailure = admin.indexOf("setError(platformApiMessage(requestError, t(\"admin.new.preprodCleanupFailed\")))");
  const reload = admin.indexOf("await onReload();");
  const committedComment = admin.indexOf("Cleanup is already committed");

  expect(cleanupFailure).toBeGreaterThan(-1);
  expect(reload).toBeGreaterThan(-1);
  expect(committedComment).toBeGreaterThan(reload);
});
