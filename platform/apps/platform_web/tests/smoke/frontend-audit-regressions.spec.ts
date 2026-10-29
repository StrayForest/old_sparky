import { existsSync, readFileSync } from "node:fs";
import { createServer, get as httpGet } from "node:http";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";
import { getTournamentWorkspace, PlatformApiError } from "../../lib/platform-api";
import { categoryForFrame } from "../../lib/cpu-profile-categories.mjs";
import {
  htmlSsrCaptureDigest,
  isHtmlSsrCaptureEligible,
  SSR_CAPTURE_SAMPLE_SCHEME,
} from "../../lib/ssr-capture-sampling.mjs";
import { cancelServerAuthFetchTerminalResponse } from "../../lib/server-fetch-response";
import { formatWorkspaceApiErrorDiagnostic } from "../../lib/ssr-error-diagnostic";
import { hasAuthorizedDiagnosticRunMarker } from "../../lib/performance-diagnostic-marker";
import {
  claimWorkspaceCpuWindowStart,
  scheduleAuthorizedWorkspaceCpuWindowEdges,
  scheduleWorkspaceCpuWindowStart,
} from "../../lib/workspace-cpu-window-scheduler.mjs";
import {
  readServerAuthJsonResponse,
  SERVER_AUTH_RESPONSE_MAX_BYTES,
} from "../../lib/server-auth-http-response";
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

test("server internal read admission diagnostics measure queued and rejected waits", async () => {
  const scheduler = new ServerInternalReadScheduler({
    activeLimit: 1,
    pendingLimit: 1,
    queueDeadlineMs: 500,
  });
  const releaseFirst = deferred();
  const firstStarted = deferred();
  const observations: Array<{ waitMs: number; outcome: string }> = [];
  const observe = (waitMs: number, outcome: string) => {
    observations.push({ waitMs, outcome });
  };

  const first = scheduler.run(async () => {
    firstStarted.resolve();
    await releaseFirst.promise;
  }, undefined, observe);
  await firstStarted.promise;
  const queued = scheduler.run(async () => undefined, undefined, observe);
  await expect(scheduler.run(async () => undefined, undefined, observe)).rejects.toMatchObject({
    name: "ServerInternalReadQueueError",
    failure: "full",
  } satisfies Partial<ServerInternalReadQueueError>);
  expect(observations).toHaveLength(2);
  expect(observations.map(({ outcome }) => outcome)).toEqual(["ok", "error"]);
  expect(observations.every(({ waitMs }) => Number.isFinite(waitMs) && waitMs >= 0)).toBe(true);

  releaseFirst.resolve();
  await Promise.all([first, queued]);
  expect(observations).toHaveLength(3);
  expect(observations.map(({ outcome }) => outcome)).toEqual(["ok", "error", "ok"]);
  expect(observations.every(({ waitMs }) => Number.isFinite(waitMs) && waitMs >= 0)).toBe(true);
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

test("workspace subspans preserve fetch and body-parse failure outcomes", async () => {
  const originalFetch = globalThis.fetch;
  const stages: Array<{ stage: string; outcome: string }> = [];
  globalThis.fetch = async () => new Response("not-json", { status: 200 });
  try {
    await expect(getTournamentWorkspace("private-fixture", {}, {
      workspaceView: "detail",
      ssrDiagnostics: {
        measureStage: async (stage, operation) => {
          try {
            const result = await operation();
            stages.push({ stage, outcome: "ok" });
            return result;
          } catch (error) {
            stages.push({ stage, outcome: "error" });
            throw error;
          }
        },
      },
    })).rejects.toThrow();
    expect(stages).toEqual([
      { stage: "workspace_http_fetch", outcome: "ok" },
      { stage: "workspace_body_parse", outcome: "error" },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("native auth transport keeps eager malformed non-success JSON semantics", () => {
  const auth = source("lib/server-auth.ts");
  const responseSource = source("lib/server-auth-http-response.ts");
  const responseEnd = responseSource.slice(responseSource.indexOf("function responseEnd(): void"));
  const parseIndex = responseEnd.indexOf("JSON.parse(bodyText)");
  const resolveIndex = responseEnd.indexOf("resolve({ status, ok, json: async () => payload })");

  expect(parseIndex).toBeGreaterThanOrEqual(0);
  expect(parseIndex).toBeLessThan(resolveIndex);
  expect(responseEnd).toContain("historical eager parse on every");
  expect(responseEnd).toContain("status. Malformed 401/403 bodies");
  expect(responseEnd).toContain("reject(error instanceof Error ? error : new Error(\"Invalid server auth response.\"))");
  expect(auth).toContain("readServerAuthJsonResponse(response, jsonParseObserver)");
  expect(auth).toContain("response.status === 401 || response.status === 403");
});

test("native auth destroys a declared oversized stalled response promptly", async () => {
  const upstreamClosed = deferred<void>();
  const server = createServer((_request, response) => {
    response.on("error", () => {});
    response.on("close", () => upstreamClosed.resolve());
    response.writeHead(200, {
      "content-length": String(SERVER_AUTH_RESPONSE_MAX_BYTES + 1),
    });
    response.write("{");
  });
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.closeAllConnections();
    throw new Error("The synthetic auth server did not bind a TCP port.");
  }

  try {
    const responseResult = new Promise<Awaited<ReturnType<typeof readServerAuthJsonResponse>>>(
      (resolveResponse, rejectResponse) => {
        const request = httpGet({
          host: "127.0.0.1",
          port: address.port,
          path: "/api/v1/users/me",
        }, (response) => {
          void readServerAuthJsonResponse(response).then(resolveResponse, rejectResponse);
        });
        request.on("error", rejectResponse);
      },
    );
    await expect(responseResult).rejects.toThrow("Server auth response exceeded its size limit.");

    let deadline: ReturnType<typeof setTimeout> | undefined;
    const closed = await Promise.race([
      upstreamClosed.promise.then(() => true),
      new Promise<boolean>((resolveDeadline) => {
        deadline = setTimeout(() => resolveDeadline(false), 2_000);
      }),
    ]);
    if (deadline !== undefined) clearTimeout(deadline);
    expect(closed).toBe(true);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
  }
});

test("native auth keeps late errors handled after a premature response close", async () => {
  let clientResponse: import("node:http").IncomingMessage | undefined;
  const server = createServer((_request, response) => {
    response.on("error", () => {});
    response.writeHead(200, { "content-length": "32" });
    response.write("{", () => {
      setTimeout(() => response.socket?.destroy(), 20);
    });
  });
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.closeAllConnections();
    throw new Error("The synthetic auth server did not bind a TCP port.");
  }

  try {
    const responseResult = new Promise<Awaited<ReturnType<typeof readServerAuthJsonResponse>>>(
      (resolveResponse, rejectResponse) => {
        const request = httpGet({
          host: "127.0.0.1",
          port: address.port,
          path: "/api/v1/auth/bootstrap",
        }, (response) => {
          clientResponse = response;
          void readServerAuthJsonResponse(response).then(resolveResponse, rejectResponse);
        });
        request.on("error", rejectResponse);
      },
    );
    await expect(responseResult).rejects.toThrow();
    expect(clientResponse).toBeDefined();
    expect(clientResponse?.complete).toBe(false);
    expect(clientResponse?.listenerCount("error")).toBeGreaterThan(0);
    expect(() => clientResponse?.emit("error", new Error("late socket reset"))).not.toThrow();
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
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
      "x-platform-ssr-request-start-ms": "1700000000000",
      "x-platform-timeout-diagnostic-id": "tdiag-123-00001",
    };
    const response = proxy(new NextRequest("https://old-sparky.com/tournaments/fixture", {
      headers: requestHeaders,
    }));
    expect(response.headers.get("x-middleware-request-x-platform-ssr-trace")).toBe("0");
    expect(response.headers.get("x-middleware-request-x-request-id")).toBe("nginx-request-id");
    expect(response.headers.get("x-middleware-request-x-platform-ssr-request-id")).toBeNull();
    expect(response.headers.get("x-middleware-request-x-platform-ssr-cf-ray")).toBeNull();
    const untrustedRequestStart = Number(
      response.headers.get("x-middleware-request-x-platform-ssr-request-start-ms"),
    );
    expect(Number.isSafeInteger(untrustedRequestStart)).toBe(true);
    expect(untrustedRequestStart).toBeGreaterThan(1_700_000_000_000);
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
      headers: {
        "x-platform-ssr-trace": runId,
        "x-platform-ssr-request-start-ms": "1",
      },
    }));
    expect(accepted.headers.get("x-middleware-request-x-platform-ssr-trace")).toBe(runId);
    expect(accepted.headers.get("x-platform-ssr-trace")).toBeNull();
    const trustedRequestStart = Number(
      accepted.headers.get("x-middleware-request-x-platform-ssr-request-start-ms"),
    );
    expect(Number.isSafeInteger(trustedRequestStart)).toBe(true);
    expect(trustedRequestStart).toBeGreaterThan(1);

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
  expect(planModule).toContain("releaseMetadataPathFromStandaloneCwd(process.cwd())");
  expect(planModule).toContain('const WORKLOAD_HTML_SSR_CAPACITY = "authenticated_html_ssr_capacity_v1";');
  expect(planModule).toContain("plan.workload !== WORKLOAD_HTML_SSR_CAPACITY");
  expect(planModule).toContain("&& plan.workload !== WORKLOAD_HTML_SSR_CAPACITY");
  expect(planModule).toContain('const WORKLOAD_HTML_SSR_CAPACITY_120S = "authenticated_html_ssr_capacity_120s_v1";');
  expect(planModule).toContain('const WORKLOAD_HTML_SSR_CPU_PROFILE_20S = "authenticated_html_ssr_cpu_profile_20s_v1";');
  expect(planModule).toContain("const expectedOnWindow = plan.workload === WORKLOAD_HTML_SSR_CAPACITY_120S");
  expect(planModule).toContain("const expectedExpiry = onEnd + (typedHtmlWorkload ? 15_000 : 0)");
  expect(planModule).toContain("|| expires !== expectedExpiry");
  expect(planModule).toContain("HTML_CAPACITY_ON_WINDOW_MS = 120_000");
  expect(planModule).toContain("plan.workload === WORKLOAD_CPU_PAIR || plan.workload === WORKLOAD_HTML_SSR_CPU_PROFILE_20S");
  expect(planModule).toContain("hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)");
  expect(planModule.indexOf("if (!hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id))"))
    .toBeLessThan(planModule.indexOf("diagnosticStorage.run({ plan, phase }, operation)"));
  expect(planModule).toContain("scheduleAuthorizedWorkspaceCpuWindowEdges(");
  expect(planModule.indexOf("scheduleAuthorizedWorkspaceCpuWindowEdges("))
    .toBeLessThan(planModule.indexOf("diagnosticPrearmStorage.run(freshPlan, operation)"));
  const rootLayout = source("app/layout.tsx");
  const observability = source("lib/server-ssr-observability.ts");
  const streamGuard = source("server-shutdown-guard.cjs");
  expect(rootLayout).toContain("return runWithSsrTrace(requestHeaders, async () => {");
  expect(rootLayout.indexOf("await connection()")).toBeLessThan(rootLayout.indexOf("performance.now()"));
  expect(rootLayout.indexOf("const [requestHeaders, requestCookies]")).toBeLessThan(rootLayout.indexOf("return runWithSsrTrace("));
  expect(rootLayout).toContain('measureSsrStage("auth_bootstrap"');
  expect(rootLayout).not.toContain("isSsrDiagnosticsEnabled");
  expect(observability).toContain("runWithSsrDiagnosticPlan(requestHeaders, async () => {");
  expect(observability).toContain("if (!enabled && !hasDiagnosticCandidate) {");
  expect(observability.indexOf("if (!enabled && !hasDiagnosticCandidate)")).toBeLessThan(
    observability.indexOf("return runWithSsrDiagnosticPlan(requestHeaders, async () => {")
  );
  expect(observability).toContain('requestHeaders.get("x-platform-ssr-trace")');
  expect(observability).toContain("isCurrentTypedHtmlSsrDiagnosticPlan()");
  expect(observability).toContain("trace.typedDiagnosticPlan\n      && trace.diagnosticRunId !== null\n      && trace.requestStartedAtMs !== null");
  expect(observability).toContain("correlationHeaders.set(SSR_REQUEST_START_HEADER, String(trace.requestStartedAtMs))");
  expect(planModule).toContain("export function isCurrentTypedHtmlSsrDiagnosticPlan(): boolean");
  expect(streamGuard).toContain('const captureSampleScheme = "sha256-runid-requestid-1of8-v1";');
  expect(streamGuard).toContain("const maxRowsPerPlan = 6_000;");
  expect(streamGuard).toContain("if (planSelection && !planSelection.captureEligible) return false;");
  expect(streamGuard).toContain('setHeader("x-platform-ssr-request-id", entry.requestId)');
  expect(streamGuard).toContain("ssr_stream_capture_coverage scheme=${captureSampleScheme}");
  expect(observability).toContain("current_diagnostic_run_id() === null");
  expect(planModule).toContain("plan.workload !== WORKLOAD_HTML_SSR_CAPACITY_120S");
  expect(planModule).toContain("plan.workload !== WORKLOAD_HTML_SSR_CPU_PROFILE_20S");
});

test("web CPU profile categories classify route, React render, chunks, and JSON by path", () => {
  const routeModule = "/srv/release/apps/platform_web/.next/server/app/(site)/tournaments/[slug]/page.js";
  expect(categoryForFrame(routeModule, "")).toBe("repo.ssr_route_module");
  expect(categoryForFrame(routeModule, "e")).toBe("repo.ssr_route_module");
  expect(categoryForFrame(routeModule, "getTournamentWorkspace")).toBe("repo.workspace_api_fetch");
  expect(categoryForFrame(
    "/srv/release/apps/platform_web/node_modules/next/dist/compiled/react-server-dom-webpack/cjs/server.node.js",
    "",
  )).toBe("react_server_render");
  expect(categoryForFrame(
    "/srv/release/apps/platform_web/node_modules/react-dom/cjs/react-dom-server.node.production.js",
    "",
  )).toBe("react_server_render");
  expect(categoryForFrame("/srv/release/apps/platform_web/.next/server/chunks/143.js", "")).toBe("compiled_server_chunk");
  expect(categoryForFrame("node:internal/json_parse", "")).toBe("serialization_validation");
  expect(categoryForFrame("/srv/release/apps/platform_web/.next/server/app/layout.js", "")).toBe("compiled_server_chunk");
});

test("typed HTML capture sampling matches the fixed cross-service SHA-256 vectors", () => {
  const runId = "0123456789abcdef0123456789abcdef";
  const eligibleRequestId = "00000000000000000000000000000002";
  const ineligibleRequestId = "11111111111111111111111111111111";
  expect(SSR_CAPTURE_SAMPLE_SCHEME).toBe("sha256-runid-requestid-1of8-v1");
  expect(htmlSsrCaptureDigest(runId, eligibleRequestId))
    .toBe("a078d044988363164c156d124496875ff00154849f1e9bd69494d0d35c1e5543");
  expect(isHtmlSsrCaptureEligible(runId, eligibleRequestId)).toBe(true);
  expect(htmlSsrCaptureDigest(runId, ineligibleRequestId))
    .toBe("3292b6e9f49cf13f86d5fa9119fb018d2de5d94882332e519636f1454bbe5380");
  expect(isHtmlSsrCaptureEligible(runId, ineligibleRequestId)).toBe(false);
  expect(htmlSsrCaptureDigest(runId.toUpperCase(), eligibleRequestId)).toBeNull();
});

test("web CPU prearm schedules exact OFF/ON edges once and rejects stale or late starts", () => {
  const plan = {
    run_id: "0123456789abcdef0123456789abcdef",
    off_start_ms: 1_000,
    off_end_ms: 21_000,
    on_start_ms: 26_000,
    on_end_ms: 46_000,
    expires_at_ms: 46_000,
  };
  const scheduledPhases: Array<"off" | "on"> = [];
  const samePlan = (expected: typeof plan, current: typeof plan | null) => (
    current !== null && JSON.stringify(expected) === JSON.stringify(current)
  );
  expect(scheduleAuthorizedWorkspaceCpuWindowEdges(
    plan, plan, plan.run_id, samePlan,
    (_current, phase) => scheduledPhases.push(phase),
  )).toBe(true);
  expect(scheduledPhases).toEqual(["off", "on"]);
  expect(scheduleAuthorizedWorkspaceCpuWindowEdges(
    plan, plan, "untrusted-run-id", samePlan,
    (_current, phase) => scheduledPhases.push(phase),
  )).toBe(false);
  expect(scheduleAuthorizedWorkspaceCpuWindowEdges(
    plan, { ...plan, expires_at_ms: plan.expires_at_ms + 1 }, plan.run_id, samePlan,
    (_current, phase) => scheduledPhases.push(phase),
  )).toBe(false);
  expect(scheduledPhases).toEqual(["off", "on"]);

  function createHarness() {
    let nowMs = 900;
    let currentPlan: typeof plan | null = plan;
    const started = new Set<string>();
    const scheduled = new Set<string>();
    const failed = new Set<string>();
    const callbacks: Array<{ dueMs: number; callback: () => void }> = [];
    const starts: Array<{ phase: "off" | "on"; atMs: number }> = [];
    let reads = 0;
    const state = { started, scheduled, failed };
    const options = () => ({
      now: () => nowMs,
      schedule: (callback: () => void, delayMs: number) => {
        callbacks.push({ dueMs: nowMs + delayMs, callback });
        return undefined;
      },
      readPlan: () => {
        reads += 1;
        return currentPlan;
      },
      samePlan: (expected: typeof plan, current: typeof plan | null) => (
        current !== null && JSON.stringify(expected) === JSON.stringify(current)
      ),
      phaseAt: (current: typeof plan, atMs: number): "off" | "on" | null => (
        atMs >= current.off_start_ms && atMs < current.off_end_ms
          ? "off"
          : atMs >= current.on_start_ms && atMs < current.on_end_ms
            ? "on"
            : null
      ),
      startTarget: (current: typeof plan, phase: "off" | "on") => (
        phase === "off" ? current.off_start_ms : current.on_start_ms
      ),
      expiresAt: (current: typeof plan) => current.expires_at_ms,
      ...state,
      onStart: (current: typeof plan, phase: "off" | "on") => {
        if (claimWorkspaceCpuWindowStart(current, phase, state)) {
          starts.push({ phase, atMs: nowMs });
        }
      },
      startToleranceMs: 250,
    });
    const runAt = (atMs: number, fireEarly = false) => {
      nowMs = atMs;
      const due = callbacks.filter((entry, index) => (
        entry.dueMs <= atMs || (fireEarly && index === 0)
      ));
      for (const entry of due) {
        callbacks.splice(callbacks.indexOf(entry), 1);
        entry.callback();
      }
    };
    return {
      callbacks,
      currentPlan: (next: typeof plan | null) => { currentPlan = next; },
      options,
      reads: () => reads,
      runAt,
      starts,
      state,
    };
  }

  const harness = createHarness();
  expect(scheduleWorkspaceCpuWindowStart(plan, "off", harness.options())).toBe(true);
  expect(scheduleWorkspaceCpuWindowStart(plan, "on", harness.options())).toBe(true);
  expect(scheduleWorkspaceCpuWindowStart(plan, "off", harness.options())).toBe(false);
  expect(harness.starts).toEqual([]);
  expect(harness.reads()).toBe(0);
  expect(claimWorkspaceCpuWindowStart(plan, "off", harness.state)).toBe(false);

  harness.runAt(1_000);
  expect(harness.starts).toEqual([{ phase: "off", atMs: 1_000 }]);
  harness.runAt(26_000);
  expect(harness.starts).toEqual([
    { phase: "off", atMs: 1_000 },
    { phase: "on", atMs: 26_000 },
  ]);
  expect(scheduleWorkspaceCpuWindowStart(plan, "on", harness.options())).toBe(false);

  const early = createHarness();
  scheduleWorkspaceCpuWindowStart(plan, "off", early.options());
  early.runAt(999, true);
  expect(early.starts).toEqual([]);
  expect(early.callbacks).toHaveLength(1);
  early.runAt(1_000);
  expect(early.starts).toEqual([{ phase: "off", atMs: 1_000 }]);

  for (const { offsetMs, shouldStart } of [
    { offsetMs: 250, shouldStart: true },
    { offsetMs: 251, shouldStart: false },
  ]) {
    const late = createHarness();
    scheduleWorkspaceCpuWindowStart(plan, "off", late.options());
    late.runAt(1_000 + offsetMs);
    expect(late.starts.length === 1).toBe(shouldStart);
    expect(late.state.failed.has(`${plan.run_id}:off`)).toBe(!shouldStart);
  }

  const removed = createHarness();
  scheduleWorkspaceCpuWindowStart(plan, "off", removed.options());
  removed.currentPlan(null);
  removed.runAt(1_000);
  expect(removed.starts).toEqual([]);
  expect(removed.state.failed.has(`${plan.run_id}:off`)).toBe(true);

  const replaced = createHarness();
  scheduleWorkspaceCpuWindowStart(plan, "off", replaced.options());
  replaced.currentPlan({ ...plan, expires_at_ms: 46_001 });
  replaced.runAt(1_000);
  expect(replaced.starts).toEqual([]);
  expect(replaced.state.failed.has(`${plan.run_id}:off`)).toBe(true);

  const afterExpiry = createHarness();
  scheduleWorkspaceCpuWindowStart(plan, "on", afterExpiry.options());
  afterExpiry.runAt(plan.expires_at_ms);
  expect(afterExpiry.starts).toEqual([]);
  expect(afterExpiry.state.failed.has(`${plan.run_id}:on`)).toBe(true);
});

test("CPU memory and GC sampling is bounded to the exact diagnostic phase", () => {
  const planModule = source("lib/performance-diagnostic-plan.ts");
  const metricsModule = source("lib/server-ssr-heap-metrics.mjs");
  expect(planModule).toContain("const MEMORY_SAMPLE_LIMIT = 5;");
  expect(planModule).toContain("const MEMORY_SAMPLE_INTERVAL_MS = 4_000;");
  expect(planModule).toContain("timelineEntryDurationWithinWindow(");
  expect(planModule).toContain("new PerformanceObserver(");
  expect(planModule).toContain("formatCpuDiagnosticMemoryEvent(tokens)");
  for (const metric of [
    "memory.rss", "heap.total_heap_size", "heap.used_heap_size", "heap.heap_size_limit",
    "memory.external", "memory.arrayBuffers", "memory.current", "memory.peak", "memory.events",
  ]) {
    expect(planModule).toContain(metric);
  }
  expect(`${planModule}\n${metricsModule}`).not.toMatch(/heapdump|writeHeapSnapshot/u);
});

test("tournament workspace timing wraps the existing opt-in SSR fetch", () => {
  const detailPage = source("app/(site)/tournaments/[slug]/page.tsx");
  const api = source("lib/platform-api.ts");
  const auth = source("lib/server-auth.ts");
  const authResponse = source("lib/server-auth-http-response.ts");
  const scheduler = source("lib/server-internal-read-scheduler.ts");
  const workspaceCalls = detailPage.match(/\bgetTournamentWorkspace\s*\(/gu) ?? [];

  // The true/false branches each contain one source call, but only one branch
  // executes. The disabled path does not allocate a measurement callback.
  expect(workspaceCalls).toHaveLength(2);
  expect(detailPage).toContain("await run_with_workspace_cpu_diagnostic(async () => {");
  expect(detailPage).toContain("const ssrTraceSampled = await isSsrTraceSampled();");
  expect(detailPage).toContain('from "@/lib/performance-diagnostic-plan"');
  expect(detailPage).toContain("getServerRequestCorrelationHeaders");
  expect(detailPage).toContain("(await getServerRequestCorrelationHeaders()).forEach");
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
  expect(api).toContain("runWithServerInternalRead(");
  expect(api).toContain('measureStage("workspace_http_fetch", fetchWorkspace)');
  expect(api).toContain('"workspace_body_parse"');
  expect(api).toContain('measureStage("workspace_projection", projectWorkspace)');
  expect(api).toContain("options.ssrDiagnostics?.admissionObserver");
  expect(api).toContain("ServerInternalReadQueueError");
  expect(api).toContain('{ code: "server_read_capacity" }');
  expect(auth.match(/runWithServerInternalRead\(/gu)).toHaveLength(2);
  expect(auth.match(/getServerRequestCorrelationHeaders\(\)/gu)).toHaveLength(2);
  expect(auth).toContain("await cancelServerAuthBody(response)");
  expect(auth).toContain("cancelServerAuthFetchTerminalResponse(response as Response, fetchController)");
  expect(auth).toContain("signal: AbortSignal.any([controller.signal, timeout])");
  expect(auth).toContain("serverAuthFetchControllers.set(response, controller)");
  expect(auth).toContain("cancelServerFetchResponse(response, controller);");
  expect(auth).toContain("return { status: \"anonymous\", user: null }");
  expect(auth).toContain("return { status: \"unavailable\", user: null }");
  expect(auth).toContain('measureSsrStage("auth_http_fetch"');
  expect(auth).toContain('measureSsrStage("auth_body_parse"');
  expect(auth).toContain('measureSsrStage("auth_projection"');
  expect(auth).toContain("getSsrAdmissionStageObserver");
  expect(auth.match(/serverAuthTransport === "node" && isSsrObservabilityActive\(\)/gu))
    .toHaveLength(2);
  const nodeJsonEnd = authResponse.slice(authResponse.indexOf("function responseEnd(): void"));
  expect(nodeJsonEnd.indexOf("JSON.parse(bodyText)")).toBeGreaterThanOrEqual(0);
  expect(nodeJsonEnd.indexOf("JSON.parse(bodyText)"))
    .toBeLessThan(nodeJsonEnd.indexOf("resolve({ status, ok, json: async () => payload })"));
  expect(nodeJsonEnd).toContain('reject(error instanceof Error ? error : new Error("Invalid server auth response."))');
  expect(scheduler).toContain("activeLimit: 24");
  expect(scheduler).toContain("pendingLimit: 128");
  expect(scheduler).toContain("queueDeadlineMs: 5_000");
  expect(scheduler).toContain("notifyAdmissionObserver");
  expect(source("lib/server-ssr-observability.ts")).toContain(
    '"internal_read_admission_wait"',
  );
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
