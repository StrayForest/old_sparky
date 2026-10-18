import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";
import { getTournamentWorkspace, PlatformApiError } from "../../lib/platform-api";
import { cancelServerAuthFetchTerminalResponse } from "../../lib/server-fetch-response";
import { formatWorkspaceApiErrorDiagnostic } from "../../lib/ssr-error-diagnostic";
import { hasAuthorizedDiagnosticRunMarker } from "../../lib/performance-diagnostic-marker";
import {
  ServerInternalReadQueueError,
  ServerInternalReadScheduler
} from "../../lib/server-internal-read-scheduler";
import { proxy } from "../../proxy";

function source(relativePath: string): string {
  return readFileSync(resolve(process.cwd(), relativePath), "utf8");
}

function deferred<T = void>() {
  let resolve!: (value?: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = (value) => resolvePromise(value as T | PromiseLike<T>);
  });
  return { promise, resolve };
}

test("server internal reads share a bounded FIFO permit through body completion", async () => {
  const scheduler = new ServerInternalReadScheduler({
    activeLimit: 1,
    pendingLimit: 2,
    queueDeadlineMs: 500,
  });
  const bodyConsumed = deferred();
  const firstStarted = deferred();
  const order: string[] = [];
  const requestHeaders = ["session-a", "session-b", "session-c"];

  const first = scheduler.run(async () => {
    firstStarted.resolve();
    await bodyConsumed.promise;
    return { status: 200, cookie: requestHeaders[0] };
  });
  await firstStarted.promise;
  const second = scheduler.run(async () => {
    order.push("second");
    return { status: 503, cookie: requestHeaders[1] };
  });
  const third = scheduler.run(async () => {
    order.push("third");
    return { status: 200, cookie: requestHeaders[2] };
  });

  expect(scheduler.activeCount).toBe(1);
  expect(scheduler.pendingCount).toBe(2);
  await expect(scheduler.run(async () => "overflow")).rejects.toMatchObject({
    name: "ServerInternalReadQueueError",
    failure: "full",
  } satisfies Partial<ServerInternalReadQueueError>);

  bodyConsumed.resolve();
  await expect(first).resolves.toEqual({ status: 200, cookie: "session-a" });
  await expect(second).resolves.toEqual({ status: 503, cookie: "session-b" });
  await expect(third).resolves.toEqual({ status: 200, cookie: "session-c" });
  expect(order).toEqual(["second", "third"]);
  expect(scheduler.activeCount).toBe(0);
  expect(scheduler.pendingCount).toBe(0);
});

test("terminal server workspace responses release the permit without waiting for body cancellation", async () => {
  const originalFetch = globalThis.fetch;
  const schedulerKey = "__oldsparkyServerInternalReadScheduler__";
  const sharedGlobal = globalThis as typeof globalThis & Record<string, unknown>;
  const hadOriginalScheduler = Object.hasOwn(sharedGlobal, schedulerKey);
  const originalScheduler = sharedGlobal[schedulerKey];
  const scheduler = new ServerInternalReadScheduler({
    activeLimit: 1,
    pendingLimit: 1,
    queueDeadlineMs: 500,
  });
  sharedGlobal[schedulerKey] = scheduler;
  const cancellationStarted = deferred();
  let releaseNeverSettlingCancellation!: () => void;
  const neverSettlingCancellation = new Promise<void>((resolve) => {
    releaseNeverSettlingCancellation = resolve;
  });
  const fetchSignals: Array<AbortSignal | null | undefined> = [];
  let fetchCount = 0;

  globalThis.fetch = async (_input, init) => {
    fetchSignals.push(init?.signal);
    fetchCount += 1;
    if (fetchCount === 1) {
      return new Response(new ReadableStream({
        cancel() {
          cancellationStarted.resolve();
          return neverSettlingCancellation;
        }
      }), { status: 404 });
    }
    return new Response(new ReadableStream({
      cancel() {
        return Promise.reject(new Error("synthetic body cancellation failure"));
      }
    }), { status: 503 });
  };

  const callerController = new AbortController();
  let firstRequest: Promise<Awaited<ReturnType<typeof getTournamentWorkspace>>> | undefined;
  let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
  try {
    firstRequest = getTournamentWorkspace("missing-fixture", {}, {
      workspaceView: "detail",
      participantsLimit: 0,
      signal: callerController.signal
    });
    const deadline = new Promise<symbol>((resolve) => {
      deadlineTimer = setTimeout(() => resolve(Symbol.for("cancel-did-not-settle")), 250);
    });
    const missing = await Promise.race([firstRequest, deadline]);
    if (typeof missing === "symbol") {
      releaseNeverSettlingCancellation();
      await firstRequest;
      throw new Error("The server workspace result waited for body cancellation.");
    }
    if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
    await cancellationStarted.promise;
    expect(missing).toBeNull();
    expect(scheduler.activeCount).toBe(0);
    expect(scheduler.pendingCount).toBe(0);
    expect(fetchSignals[0]?.aborted).toBe(true);
    expect(callerController.signal.aborted).toBe(false);

    await expect(getTournamentWorkspace("unavailable-fixture", {}, {
      workspaceView: "detail",
      participantsLimit: 0
    })).rejects.toMatchObject({
      name: "PlatformApiError",
      status: 503
    } satisfies Partial<PlatformApiError>);
    expect(fetchSignals[1]?.aborted).toBe(true);
    expect(fetchCount).toBe(2);
    expect(scheduler.activeCount).toBe(0);
    expect(scheduler.pendingCount).toBe(0);
  } finally {
    if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
    releaseNeverSettlingCancellation();
    globalThis.fetch = originalFetch;
    try {
      if (firstRequest) await firstRequest;
    } finally {
      if (hadOriginalScheduler) {
        sharedGlobal[schedulerKey] = originalScheduler;
      } else {
        delete sharedGlobal[schedulerKey];
      }
    }
  }
});

test("terminal server auth fetch responses retain snapshots without awaiting body cancellation", async () => {
  const cases = [
    { status: 401, snapshot: { status: "anonymous", user: null } },
    { status: 403, snapshot: { status: "anonymous", user: null } },
    { status: 503, snapshot: { status: "unavailable", user: null } },
  ] as const;

  for (const { status, snapshot } of cases) {
    const scheduler = new ServerInternalReadScheduler({
      activeLimit: 1,
      pendingLimit: 0,
      queueDeadlineMs: 500,
    });
    const cancellationStarted = deferred();
    const controller = new AbortController();
    const response = new Response(new ReadableStream({
      cancel() {
        cancellationStarted.resolve();
        return new Promise<void>(() => {});
      }
    }), { status });

    const result = await scheduler.run(async () =>
      cancelServerAuthFetchTerminalResponse(response, controller)
    );

    await cancellationStarted.promise;
    expect(result).toEqual(snapshot);
    expect(controller.signal.aborted).toBe(true);
    expect(scheduler.activeCount).toBe(0);
    expect(scheduler.pendingCount).toBe(0);
  }
});

test("server internal read waiters are removed on abort/deadline and permits release on error", async () => {
  const scheduler = new ServerInternalReadScheduler({
    activeLimit: 1,
    pendingLimit: 2,
    queueDeadlineMs: 20,
  });
  const hold = deferred();
  const entered = deferred();
  const active = scheduler.run(async () => {
    entered.resolve();
    await hold.promise;
  });
  await entered.promise;

  const controller = new AbortController();
  const aborted = scheduler.run(async () => undefined, controller.signal);
  controller.abort(new Error("request cancelled"));
  await expect(aborted).rejects.toThrow("request cancelled");
  expect(scheduler.pendingCount).toBe(0);

  const expired = scheduler.run(async () => undefined);
  await expect(expired).rejects.toMatchObject({
    name: "ServerInternalReadQueueError",
    failure: "deadline",
  });
  expect(scheduler.pendingCount).toBe(0);

  hold.resolve();
  await active;
  await expect(scheduler.run(async () => {
    throw new Error("upstream failure");
  })).rejects.toThrow("upstream failure");
  expect(scheduler.activeCount).toBe(0);

  const delayedTimerScheduler = new ServerInternalReadScheduler({
    activeLimit: 1,
    pendingLimit: 2,
    queueDeadlineMs: 25,
  });
  const delayedHold = deferred();
  const delayedActiveStarted = deferred();
  const delayedActive = delayedTimerScheduler.run(async () => {
    delayedActiveStarted.resolve();
    await delayedHold.promise;
  });
  await delayedActiveStarted.promise;

  let expiredOperationStarted = false;
  const expiredBeforeDrain = delayedTimerScheduler.run(async () => {
    expiredOperationStarted = true;
  });

  // Block this event loop after the waiter deadline so its timer callback
  // cannot run before releasing the active permit and draining the queue.
  Atomics.wait(
    new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT)),
    0,
    0,
    60,
  );
  let nextOperationStarted = false;
  const nextOperation = delayedTimerScheduler.run(async () => {
    nextOperationStarted = true;
  });
  delayedHold.resolve();

  await delayedActive;
  await expect(expiredBeforeDrain).rejects.toMatchObject({
    name: "ServerInternalReadQueueError",
    failure: "deadline",
  } satisfies Partial<ServerInternalReadQueueError>);
  await expect(nextOperation).resolves.toBeUndefined();
  expect(expiredOperationStarted).toBe(false);
  expect(nextOperationStarted).toBe(true);
  expect(delayedTimerScheduler.activeCount).toBe(0);
  expect(delayedTimerScheduler.pendingCount).toBe(0);
});

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

test("CPU memory and GC sampling is bounded to the exact diagnostic phase", () => {
  const planModule = source("lib/performance-diagnostic-plan.ts");
  expect(planModule).toContain("const MEMORY_SAMPLE_LIMIT = 5;");
  expect(planModule).toContain("const MEMORY_SAMPLE_INTERVAL_MS = 4_000;");
  expect(planModule).toContain("timelineEntryDurationWithinWindow(");
  expect(planModule).toContain("new PerformanceObserver(");
  expect(planModule).toContain("cpu_diagnostic_memory service=web run_id=");
  for (const metric of [
    "memory.rss", "heap.total_heap_size", "heap.used_heap_size", "heap.heap_size_limit",
    "memory.external", "memory.arrayBuffers", "memory.current", "memory.peak", "memory.events",
  ]) {
    expect(planModule).toContain(metric);
  }
  expect(planModule).not.toMatch(/heapdump|writeHeapSnapshot/u);
});

test("tournament workspace timing wraps the existing opt-in SSR fetch", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const api = source("lib/platform-api.ts");
  const auth = source("lib/server-auth.ts");
  const scheduler = source("lib/server-internal-read-scheduler.ts");
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
  expect(api).toContain("typeof window === \"undefined\"");
  expect(api).toContain("runWithServerInternalRead(loadWorkspace, options.signal)");
  expect(api).toContain("ServerInternalReadQueueError");
  expect(api).toContain('{ code: "server_read_capacity" }');
  expect(auth.match(/runWithServerInternalRead\(/gu)).toHaveLength(2);
  expect(auth).toContain("await cancelServerAuthBody(response)");
  expect(auth).toContain("cancelServerAuthFetchTerminalResponse(response as Response, fetchController)");
  expect(auth).toContain("signal: AbortSignal.any([controller.signal, timeout])");
  expect(auth).toContain("serverAuthFetchControllers.set(response, controller)");
  expect(auth).toContain("cancelServerFetchResponse(response, controller);");
  expect(auth).toContain("return { status: \"anonymous\", user: null }");
  expect(auth).toContain("return { status: \"unavailable\", user: null }");
  expect(scheduler).toContain("activeLimit: 24");
  expect(scheduler).toContain("pendingLimit: 128");
  expect(scheduler).toContain("queueDeadlineMs: 5_000");
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
