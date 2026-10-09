import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";
import { formatWorkspaceApiErrorDiagnostic } from "../../lib/ssr-error-diagnostic";
import { hasAuthorizedDiagnosticRunMarker } from "../../lib/performance-diagnostic-marker";
import { proxy } from "../../proxy";

function source(relativePath: string): string {
  return readFileSync(resolve(process.cwd(), relativePath), "utf8");
}

test("public timeout metadata cannot promote SSR trace sampling", () => {
  const priorEnabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED;
  const priorRate = process.env.PLATFORM_SSR_PERF_SAMPLE_RATE;
  process.env.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
  process.env.PLATFORM_SSR_PERF_SAMPLE_RATE = "0.05";
  try {
    const requestHeaders = {
      "x-request-id": "nginx-request-id",
      "cf-ray": "trusted-edge-ray",
      "x-platform-ssr-trace": "1",
      "x-platform-ssr-request-id": "forged-request-id",
      "x-platform-ssr-cf-ray": "forged-cf-ray",
      "x-platform-timeout-diagnostic-id": "tdiag-123-00001",
    };
    const response = proxy(new NextRequest("https://old-sparky.com/tournaments/fixture", {
      headers: requestHeaders,
    }));
    expect(response.headers.get("x-middleware-request-x-platform-ssr-trace")).toBe("0");
    expect(response.headers.get("x-middleware-request-x-request-id")).toBe("nginx-request-id");
    expect(response.headers.get("x-middleware-request-x-platform-ssr-request-id")).toBeNull();
    expect(response.headers.get("x-middleware-request-x-platform-ssr-cf-ray")).toBeNull();
    expect(response.headers.get("x-middleware-request-x-platform-timeout-diagnostic-id"))
      .toBeNull();

    const cfFallback = proxy(new NextRequest("https://old-sparky.com/tournaments/fixture", {
      headers: {
        "cf-ray": "trusted-edge-ray",
        "x-platform-timeout-diagnostic-id": "tdiag-999999-00001",
      },
    }));
    expect(cfFallback.headers.get("x-middleware-request-x-platform-ssr-trace")).toBe("0");
    expect(cfFallback.headers.get("x-middleware-request-cf-ray")).toBe("trusted-edge-ray");
    expect(cfFallback.headers.get("x-middleware-request-x-platform-timeout-diagnostic-id"))
      .toBeNull();
  } finally {
    if (priorEnabled === undefined) delete process.env.PLATFORM_SSR_PERF_LOG_ENABLED;
    else process.env.PLATFORM_SSR_PERF_LOG_ENABLED = priorEnabled;
    if (priorRate === undefined) delete process.env.PLATFORM_SSR_PERF_SAMPLE_RATE;
    else process.env.PLATFORM_SSR_PERF_SAMPLE_RATE = priorRate;
  }
});

test("only a plan-shaped internal trace marker reaches server rendering", () => {
  const priorEnabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED;
  const priorRate = process.env.PLATFORM_SSR_PERF_SAMPLE_RATE;
  process.env.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
  process.env.PLATFORM_SSR_PERF_SAMPLE_RATE = "0.05";
  try {
    const runId = "0123456789abcdef0123456789abcdef";
    const accepted = proxy(new NextRequest("https://old-sparky.com/tournaments/fixture", {
      headers: { "x-platform-ssr-trace": runId },
    }));
    expect(accepted.headers.get("x-middleware-request-x-platform-ssr-trace")).toBe(runId);
    expect(accepted.headers.get("x-platform-ssr-trace")).toBeNull();

    const malformed = proxy(new NextRequest("https://old-sparky.com/tournaments/fixture", {
      headers: { "x-platform-ssr-trace": `${runId}extra` },
    }));
    expect(malformed.headers.get("x-middleware-request-x-platform-ssr-trace"))
      .not.toBe(`${runId}extra`);
  } finally {
    if (priorEnabled === undefined) delete process.env.PLATFORM_SSR_PERF_LOG_ENABLED;
    else process.env.PLATFORM_SSR_PERF_LOG_ENABLED = priorEnabled;
    if (priorRate === undefined) delete process.env.PLATFORM_SSR_PERF_SAMPLE_RATE;
    else process.env.PLATFORM_SSR_PERF_SAMPLE_RATE = priorRate;
  }
});

test("workspace CPU diagnostics require an exact private run marker", () => {
  const runId = "0123456789abcdef0123456789abcdef";
  expect(hasAuthorizedDiagnosticRunMarker(runId, runId)).toBe(true);
  for (const value of [null, "1", runId.toUpperCase(), `${runId}0`, "f".repeat(32)]) {
    expect(hasAuthorizedDiagnosticRunMarker(value, runId)).toBe(false);
  }
  const planModule = source("lib/performance-diagnostic-plan.ts");
  expect(planModule).toContain('resolve(process.cwd(), "../../..", "RELEASE.json")');
  expect(planModule).toContain("hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)");
  expect(planModule.indexOf("if (!hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id))"))
    .toBeLessThan(planModule.indexOf("diagnosticStorage.run({ plan, phase }, operation)"));
});

test("tournament workspace timing wraps the existing opt-in SSR fetch", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const workspaceCalls = detailPage.match(/\bgetTournamentWorkspace\s*\(/gu) ?? [];

  // The true/false branches each contain one source call, but only one branch
  // executes. The disabled path does not allocate a measurement callback.
  expect(workspaceCalls).toHaveLength(2);
  expect(detailPage).toContain("await run_with_workspace_cpu_diagnostic(async () => {");
  expect(detailPage).toContain("const ssrTraceSampled = await isSsrTraceSampled();");
  expect(detailPage).toContain('from "@/lib/performance-diagnostic-plan"');
  expect(detailPage).toContain("getServerRequestCorrelationHeaders");
  expect(detailPage).toContain("workspaceHeaders[name] = value;");
  expect(detailPage).toContain('? measureSsrStage("tournament_workspace", () =>');
  expect(detailPage).toContain("const workspace = await (ssrTraceSampled");
  expect(detailPage).toContain(': getTournamentWorkspace(slug, workspaceHeaders, workspaceOptions));');
  expect(detailPage).toContain('recordSsrPoint("tournament_detail_data_ready")');
  expect(detailPage.indexOf('recordSsrPoint("tournament_detail_data_ready")'))
    .toBeGreaterThan(detailPage.indexOf("initialTournament = workspace.tournament"));
  expect(detailPage).toContain("const workspace = await (ssrTraceSampled");
  expect(detailPage).toContain("? measureSsrStage(");
  expect(detailPage).toContain(": getTournamentWorkspace(slug, workspaceHeaders, workspaceOptions));");
});

test("workspace API error diagnostics emit only bounded status and correlation fields", () => {
  expect(formatWorkspaceApiErrorDiagnostic({
    requestId: "edge-request-1",
    cfRay: "edge-ray-2",
    status: 503,
  })).toBe(
    "ssr_error request_id=edge-request-1 cf_ray=edge-ray-2"
      + " stage=tournament_workspace family=platform_api_error status=503"
      + " response_code=unavailable",
  );
  expect(formatWorkspaceApiErrorDiagnostic({
    requestId: "edge-request-1",
    cfRay: "edge-ray-2",
    status: 503,
    diagnosticRunId: "0123456789abcdef0123456789abcdef",
  })).toContain("diagnostic_run_id=0123456789abcdef0123456789abcdef");
  expect(formatWorkspaceApiErrorDiagnostic({
    requestId: "edge-request-1",
    cfRay: "edge-ray-2",
    status: 503,
    diagnosticRunId: "not-a-plan-id",
  })).toBeNull();
  expect(formatWorkspaceApiErrorDiagnostic({
    requestId: "edge request with spaces",
    cfRay: "edge-ray-2",
    status: 503,
  })).toBeNull();
  expect(formatWorkspaceApiErrorDiagnostic({
    requestId: "edge-request-1",
    cfRay: "edge-ray-2",
    status: 200,
  })).toBeNull();

  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const observability = source("lib/server-ssr-observability.ts");
  expect(observability).toContain(
    'process.env.PLATFORM_SSR_WORKSPACE_ERROR_DIAGNOSTIC === "true"',
  );
  expect(observability).toContain("current_diagnostic_run_id() === null");
  expect(observability).toContain("diagnostic_run_id=${trace.diagnosticRunId}");
  expect(detailPage).toContain("await recordSsrWorkspaceApiError(error.status)");
  expect(detailPage).toContain("throw error;");
});

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
  expect(detailClientPage).toContain("initialRequest.retryGeneration === retryGeneration");
  expect(detailClientPage).toContain("stateContextRef");
  expect(detailClientPage).toContain("sameDetailContext");
  expect(detailClientPage).toContain("const displayState");
  expect(detailClientPage).toContain("lifecycleGenerationRef");
  expect(detailClientPage).toContain("nextLifecycleGeneration");
  expect(detailClientPage).toContain("data-testid=\"tournament-detail-lifecycle\"");
  expect(detailClientPage).toContain("data-settled={settled ? \"true\" : \"false\"}");
  expect(detailClientPage).not.toContain("data-slug");
  expect(detailClientPage).not.toContain("data-invite");
  expect(detailClientPage).not.toContain("data-session");
  expect(detailClientPage).toContain("key={serverSeedVersion}");
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
