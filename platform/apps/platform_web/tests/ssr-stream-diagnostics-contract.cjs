"use strict";

const childProcess = process.getBuiltinModule("node:child_process");
const fs = process.getBuiltinModule("node:fs");
const path = process.getBuiltinModule("node:path");

const guardPath = path.resolve(__dirname, "..", "server-shutdown-guard.cjs");
const fixtureWatchdogMs = 3_000;
const behaviorTimeoutMs = 5_000;
const cleanupTimeoutMs = 1_000;

function capacityCloseFixtureSource() {
  return `
const http = process.getBuiltinModule("node:http");
const crypto = process.getBuiltinModule("node:crypto");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
const runId = "0123456789abcdef0123456789abcdef";
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
function captureEligible(requestId) {
  return (crypto.createHash("sha256").update(runId + ":" + requestId, "ascii").digest()[0] & 7) === 0;
}
const noneligibleIds = [];
let eligibleId = null;
for (let candidate = 0; noneligibleIds.length < 128 || eligibleId === null; candidate += 1) {
  const requestId = candidate.toString(16).padStart(32, "0");
  if (captureEligible(requestId)) {
    if (eligibleId === null) eligibleId = requestId;
  } else if (noneligibleIds.length < 128) {
    noneligibleIds.push(requestId);
  }
}
const closedTags = new Set();
const closeWaiters = new Map();
function waitForClose(tag) {
  if (closedTags.has(tag)) return Promise.resolve();
  return new Promise((resolve) => closeWaiters.set(tag, resolve));
}
async function sendSelection(port, tag, requestId) {
  const closed = waitForClose(tag);
  const response = await fetch("http://127.0.0.1:" + port + "/select", {
    headers: {
      "x-request-id": requestId,
      "x-platform-ssr-trace": runId,
      "x-fixture-tag": tag,
      accept: "text/html",
    },
  });
  await response.text();
  await closed;
  return response.headers.get("x-fixture-selector-result");
}
const server = http.createServer((request, response) => {
  const requestId = request.headers["x-request-id"];
  const accepted = selectPlanRequest({
    requestId,
    runId,
    expiresAtMs: Date.now() + 10_000,
    captureEligible: captureEligible(requestId),
  });
  response.setHeader("x-fixture-selector-result", accepted ? "accepted" : "rejected");
  response.once("close", () => {
    const tag = request.headers["x-fixture-tag"];
    closedTags.add(tag);
    closeWaiters.get(tag)?.();
    closeWaiters.delete(tag);
  });
  response.writeHead(200, { "content-type": "text/html" });
  response.end("ok");
});
server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  try {
    for (let index = 0; index < noneligibleIds.length; index += 1) {
      const result = await sendSelection(port, "closed-noneligible-" + index, noneligibleIds[index]);
      if (result !== "accepted") throw new Error("closed noneligible selection was refused at index " + index);
    }
    const eligibleResult = await sendSelection(port, "eligible-after-closed-noneligible", eligibleId);
    if (eligibleResult !== "accepted") throw new Error("eligible selection was starved after closed noneligible responses");
    const eligibleAgain = await sendSelection(port, "eligible-after-close", eligibleId);
    if (eligibleAgain !== "accepted") throw new Error("eligible selection was not removed on response close");
    console.log("closed_noneligible=128 eligible_after_close=accepted eligible_again=accepted");
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
`;
}

function duplicateSelectionCloseFixtureSource() {
  return `
const http = process.getBuiltinModule("node:http");
const crypto = process.getBuiltinModule("node:crypto");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
const firstRunId = "0123456789abcdef0123456789abcdef";
const secondRunId = "fedcba9876543210fedcba9876543210";
let requestId = null;
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
for (let candidate = 0; requestId === null; candidate += 1) {
  const candidateId = candidate.toString(16).padStart(32, "0");
  const firstEligible = (crypto.createHash("sha256").update(firstRunId + ":" + candidateId, "ascii").digest()[0] & 7) === 0;
  const secondEligible = (crypto.createHash("sha256").update(secondRunId + ":" + candidateId, "ascii").digest()[0] & 7) === 0;
  if (firstEligible && secondEligible) {
    requestId = candidateId;
  }
}
let firstResponse = null;
let secondResponse = null;
let firstClosed = false;
function finishSecond() {
  if (!firstClosed || !secondResponse || secondResponse.headersSent) return;
  secondResponse.writeHead(200, { "content-type": "text/html" });
  secondResponse.end("second");
}
const server = http.createServer((request, response) => {
  const tag = request.headers["x-fixture-tag"];
  const runId = tag === "first" ? firstRunId : secondRunId;
  const accepted = selectPlanRequest({
    requestId,
    runId,
    expiresAtMs: Date.now() + 10_000,
    captureEligible: true,
  });
  response.setHeader("x-fixture-selector-result", accepted ? "accepted" : "rejected");
  if (tag === "first") {
    firstResponse = response;
    response.writeHead(200, { "content-type": "text/html" });
    response.write("first-open");
    response.once("close", () => {
      firstClosed = true;
      finishSecond();
    });
    return;
  }
  if (tag === "second") {
    secondResponse = response;
    firstResponse?.end("first-close");
    finishSecond();
    return;
  }
  response.writeHead(404);
  response.end();
});
server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  try {
    const commonHeaders = { "x-request-id": requestId, accept: "text/html" };
    const first = await fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
      headers: { ...commonHeaders, "x-platform-ssr-trace": firstRunId, "x-fixture-tag": "first" },
    });
    if (first.status !== 200 || first.headers.get("x-fixture-selector-result") !== "accepted") {
      throw new Error("first duplicate-id selection failed");
    }
    const firstBody = first.text();
    const second = await fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
      headers: { ...commonHeaders, "x-platform-ssr-trace": secondRunId, "x-fixture-tag": "second" },
    });
    if (second.status !== 200 || second.headers.get("x-fixture-selector-result") !== "accepted") {
      throw new Error("replacement duplicate-id selection failed");
    }
    await Promise.all([firstBody, second.text()]);
    console.log("duplicate_id_replacement=accepted");
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
`;
}

function bounded(value) {
  return String(value || "").slice(0, 4_000);
}

function withTimeout(promise, timeoutMs) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function signalProcessGroup(child, signal) {
  if (child.exitCode !== null || child.signalCode !== null || !child.pid) {
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error && error.code !== "ESRCH") {
      throw error;
    }
  }
}

function fixtureSource({ disabled, diagnosticMarker, planSelection }) {
  if (planSelection === "capacity-close") return capacityCloseFixtureSource();
  if (planSelection === "duplicate-id-close") return duplicateSelectionCloseFixtureSource();
  const planRunId = "0123456789abcdef0123456789abcdef";
  const requestId = planSelection === "valid"
    ? "00000000000000000000000000000002"
    : planSelection === "unsampled"
      ? "11111111111111111111111111111111"
      : "d".repeat(32);
  const captureEligible = (process.getBuiltinModule("node:crypto")
    .createHash("sha256")
    .update(`${planRunId}:${requestId}`, "ascii")
    .digest()[0] & 7) === 0;
  const requestMarker = planSelection ? planRunId : (diagnosticMarker ? "1" : "");
  const registration = planSelection === "valid" || planSelection === "unsampled"
    ? `const selectorExpiresAtMs = Date.now() + 10_000;
  const selectorCaptureEligible = ${captureEligible};
  if (!selectPlanRequest({requestId: "${requestId}", runId: "${planRunId}", expiresAtMs: selectorExpiresAtMs, captureEligible: selectorCaptureEligible})) {
    const expectedCaptureEligible = (process.getBuiltinModule("node:crypto")
      .createHash("sha256")
      .update("${planRunId}:${requestId}", "ascii")
      .digest()[0] & 7) === 0;
    const now = Date.now();
    throw new Error("validated request was not selected: " + JSON.stringify({
      traceCandidate: /^[0-9a-f]{32}$/.test(request.headers["x-platform-ssr-trace"] || ""),
      traceRunMatches: request.headers["x-platform-ssr-trace"] === "${planRunId}",
      requestIdMatches: request.headers["x-request-id"] === "${requestId}",
      expiryWithinBounds: Number.isSafeInteger(selectorExpiresAtMs)
        && selectorExpiresAtMs > now && selectorExpiresAtMs - now <= 160_000,
      captureEligibilityMatches: selectorCaptureEligible === expectedCaptureEligible,
      requestStartSet: Boolean(request[Symbol.for("old-sparky.ssr-request-start")]),
    }));
  }`
    : planSelection === "wrong-request"
      ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${"e".repeat(32)}", runId: "${planRunId}", expiresAtMs: Date.now() + 10_000});`
      : planSelection === "wrong-run"
        ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${requestId}", runId: "${"f".repeat(32)}", expiresAtMs: Date.now() + 10_000});`
      : planSelection === "expired"
        ? `globalThis[Symbol.for("old-sparky.ssr-stream-selector")]({requestId: "${requestId}", runId: "${planRunId}", expiresAtMs: Date.now() - 1});`
        : "";
  return `
const http = process.getBuiltinModule("node:http");
const selectPlanRequest = globalThis[Symbol.for("old-sparky.ssr-stream-selector")];
if (typeof selectPlanRequest !== "function") throw new Error("stream selector is unavailable");
for (const rejected of [
  {requestId: "bad request", runId: "${planRunId}", expiresAtMs: Date.now() + 1_000},
  {requestId: "request-unselected", runId: "bad", expiresAtMs: Date.now() + 1_000},
  {requestId: "request-unselected", runId: "${planRunId}", expiresAtMs: Date.now() + 160_001},
]) {
  if (selectPlanRequest(rejected) !== false) throw new Error("stream selector accepted unbounded or malformed selection");
}
const watchdog = setTimeout(() => {
  console.error("SSR diagnostics fixture watchdog elapsed");
  process.exit(124);
}, ${fixtureWatchdogMs});
const server = http.createServer((request, response) => {
  if (request.url !== "/tournaments/private-slug") {
    response.writeHead(404);
    response.end();
    return;
  }
  ${registration}
  console.log("request_start_marker=" + (request[Symbol.for("old-sparky.ssr-request-start")] ? "set" : "none"));
  response.writeHead(200, {"content-type": "text/html"});
  ${disabled ? 'response.end("<html></html>");' : 'response.write("<html>"); response.end("</html>");'}
});
server.on("error", (error) => {
  console.error(error);
  clearTimeout(watchdog);
  process.exitCode = 1;
});
server.listen(0, "127.0.0.1", () => {
  const port = server.address().port;
  fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
    headers: {
      accept: "text/html",
      "x-request-id": "${planSelection ? requestId : disabled ? "request-disabled" : "request-1"}",
      "cf-ray": "ray-1",
      ${diagnosticMarker || planSelection ? [
        '"x-platform-timeout-diagnostic-id": "tdiag-123-00001",',
        '"x-platform-ssr-trace": "' + requestMarker + '",',
        '"x-platform-ssr-request-id": "forged-request-id",',
        '"x-platform-ssr-cf-ray": "forged-cf-ray",',
        '"x-platform-ssr-proxy-start-ms": "1",',
        '"x-platform-ssr-request-start-ms": "1",',
      ].join("\n      ") : ""}
    },
  }).then((response) => {
    console.log("client_join=" + (response.headers.get("x-platform-ssr-request-id") || "none"));
    return response.text();
  }).then(() => {
    server.close((error) => {
      clearTimeout(watchdog);
      if (error) {
        console.error(error);
        process.exitCode = 1;
      }
    });
  }).catch((error) => {
    console.error(error);
    clearTimeout(watchdog);
    process.exitCode = 1;
    server.close();
  });
});
`;
}

async function runFixture(name, {
  disabled = false,
  diagnosticMarker = false,
  planSelection = null,
  sampleRate = "1",
} = {}) {
  const environment = { ...process.env };
  if (disabled) {
    delete environment.PLATFORM_SSR_PERF_LOG_ENABLED;
  } else {
    environment.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
    environment.PLATFORM_SSR_PERF_SAMPLE_RATE = sampleRate;
  }
  const child = childProcess.spawn(
    process.execPath,
    ["--require", guardPath, "-e", fixtureSource({ disabled, diagnosticMarker, planSelection })],
    {
      detached: true,
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  const exit = new Promise((resolve) => {
    child.once("error", (error) => resolve({ kind: "error", error }));
    child.once("close", (code, signal) => resolve({ kind: "close", code, signal }));
  });

  let failure = null;
  try {
    const result = await withTimeout(exit, behaviorTimeoutMs);
    if (result.kind === "timeout") {
      failure = `SSR diagnostics ${name} fixture exceeded behavior deadline (${behaviorTimeoutMs} ms)`;
    } else if (result.kind === "error") {
      failure = `SSR diagnostics ${name} fixture failed to start: ${result.error}`;
    } else if (result.code !== 0) {
      failure = `SSR diagnostics ${name} fixture exited with code ${result.code ?? "null"}`;
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      signalProcessGroup(child, "SIGKILL");
      const cleanup = await withTimeout(exit, cleanupTimeoutMs);
      if (cleanup.kind === "timeout" && !failure) {
        failure = `SSR diagnostics ${name} fixture cleanup exceeded deadline (${cleanupTimeoutMs} ms)`;
      }
    }
  }

  if (failure) {
    throw new Error(
      `${failure}; stdout=${JSON.stringify(bounded(stdout))} stderr=${JSON.stringify(bounded(stderr))}`,
    );
  }
  return stdout;
}

function assertCount(output, fragment, expected, name) {
  const actual = output.split(fragment).length - 1;
  if (actual !== expected) {
    throw new Error(`${name}: expected ${fragment} ${expected} time(s), got ${actual}`);
  }
}

async function main() {
  const guardSource = fs.readFileSync(guardPath, "utf8");
  for (const fragment of [
    "maxSelectedRequests = 128",
    "maxPendingRequests = 128",
    "maxRowsPerRequest = 6",
    "maxRowsPerPlan = 6_000",
    'captureSampleScheme = "sha256-runid-requestid-1of8-v1"',
    '(digest[0] & 7) === 0',
    "captureEligible: entry.captureEligible",
    "ssr_stream_capture_coverage scheme=${captureSampleScheme}",
    "selector_overflow_unattributed=",
    'setHeader("x-platform-ssr-request-id", entry.requestId)',
  ]) {
    if (!guardSource.includes(fragment)) {
      throw new Error(`SSR stream guard contract omitted ${fragment}`);
    }
  }
  const prearmModule = await import(path.resolve(
    __dirname, "..", "lib", "workspace-diagnostic-prearm.mjs",
  ));
  const pagePath = path.resolve(
    __dirname, "..", "app", "(site)", "tournaments", "[slug]", "page.tsx",
  );
  const planPath = path.resolve(__dirname, "..", "lib", "performance-diagnostic-plan.ts");
  const pageSource = fs.readFileSync(pagePath, "utf8");
  const planSource = fs.readFileSync(planPath, "utf8");
  const baseHeaders = { cookie: "session=fixture" };
  const runId = "a".repeat(32);
  const prearmPlan = {
    run_id: runId,
    workload: "authenticated_html_ssr_capacity_v1",
    off_start_ms: 100_000,
  };
  const authorizedRunId = prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
    prearmPlan, { ...prearmPlan }, 48_000,
  );
  for (const workload of ["authenticated_html_ssr_capacity_120s_v1"]) {
    const typedPlan = { ...prearmPlan, workload };
    if (prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
      typedPlan, { ...typedPlan }, 48_000,
    ) !== runId) {
      throw new Error(`workspace prearm rejected the approved typed HTML workload ${workload}`);
    }
  }
  let prearmPlanReadCount = 0;
  const absentContextRunId = prearmModule.readAuthorizedWorkspaceDiagnosticPrearmRunId(
    null,
    () => {
      prearmPlanReadCount += 1;
      return prearmPlan;
    },
    48_000,
  );
  const boundContextRunId = prearmModule.readAuthorizedWorkspaceDiagnosticPrearmRunId(
    prearmPlan,
    () => {
      prearmPlanReadCount += 1;
      return { ...prearmPlan };
    },
    48_000,
  );
  if (absentContextRunId !== null || boundContextRunId !== runId || prearmPlanReadCount !== 1) {
    throw new Error("workspace prearm plan revalidation read without context or skipped bound context");
  }
  const sampledCorrelationHeaders = {
    ...baseHeaders,
    "x-platform-ssr-trace": "1",
    "x-platform-ssr-request-id": "request-fixture",
  };
  const forwarded = prearmModule.withWorkspaceDiagnosticPrearmHeader(
    sampledCorrelationHeaders, authorizedRunId,
  );
  if (forwarded.cookie !== baseHeaders.cookie
      || forwarded["x-platform-ssr-trace"] !== runId
      || forwarded["x-platform-ssr-request-id"] !== "request-fixture"
      || Object.hasOwn(baseHeaders, "x-platform-ssr-trace")) {
    throw new Error("workspace prearm helper did not retain the exact marker after correlation headers");
  }
  const correlationWithoutPrearm = prearmModule.withWorkspaceDiagnosticPrearmHeader(
    sampledCorrelationHeaders, null,
  );
  if (correlationWithoutPrearm["x-platform-ssr-trace"] !== "1") {
    throw new Error("workspace prearm helper changed a sampled correlation marker without a plan");
  }
  for (const untrusted of [null, "1", `${runId}extra`, runId.toUpperCase()]) {
    const rejected = prearmModule.withWorkspaceDiagnosticPrearmHeader(baseHeaders, untrusted);
    if (Object.hasOwn(rejected, "x-platform-ssr-trace")) {
      throw new Error("workspace prearm helper forwarded an absent or malformed marker");
    }
  }
  for (const [currentPlan, nowMs] of [
    [null, 48_000],
    [{ ...prearmPlan, source_sha: "b".repeat(40) }, 48_000],
    [{ ...prearmPlan, workload: "authenticated_workspace_read_pair_v1" }, 48_000],
    [{ ...prearmPlan }, 100_000],
    [{ ...prearmPlan }, 47_999],
  ]) {
    if (prearmModule.authorizedWorkspaceDiagnosticPrearmRunId(
      prearmPlan, currentPlan, nowMs,
    ) !== null) {
      throw new Error("workspace prearm authorization accepted absent, replaced, wrong-scope or mistimed plan");
    }
  }
  if (!pageSource.includes("withWorkspaceDiagnosticPrearmHeader(")
      || !pageSource.includes("current_workspace_cpu_diagnostic_prearm_run_id()")) {
    throw new Error("tournament page no longer uses the validated workspace prearm helper");
  }
  const correlationMerge = pageSource.indexOf("getServerRequestCorrelationHeaders()).forEach");
  const prearmMerge = pageSource.indexOf(
    "withWorkspaceDiagnosticPrearmHeader(", pageSource.indexOf("const workspaceHeaders"),
  );
  if (correlationMerge < 0 || prearmMerge <= correlationMerge) {
    throw new Error("validated workspace prearm marker must be merged after sampled correlation headers");
  }
  for (const fragment of [
    "diagnosticPrearmStorage",
    "hasAuthorizedDiagnosticRunMarker(incomingTrace, plan.run_id)",
    "authorizedWorkspaceDiagnosticPrearmRunId(",
    "readAuthorizedWorkspaceDiagnosticPrearmRunId(",
  ]) {
    if (!planSource.includes(fragment)) {
      throw new Error(`workspace prearm plan contract omitted ${fragment}`);
    }
  }
  const prearmGetter = planSource.match(
    /export function current_workspace_cpu_diagnostic_prearm_run_id\(\): string \| null \{([\s\S]*?)\n\}/u,
  );
  if (!prearmGetter
      || !prearmGetter[1].includes("prearmPlan ?? null, safeReadPlan, Date.now()")
      || prearmGetter[1].includes("safeReadPlan()")) {
    throw new Error("workspace prearm getter must defer plan reads until its dedicated context exists");
  }

  const enabledOutput = await runFixture("enabled");
  for (const stage of [
    "response_stream_start",
    "first_body_write_attempt",
    "response_finish",
    "response_close",
  ]) {
    assertCount(enabledOutput, `stage=${stage}`, 1, "enabled SSR diagnostics");
  }
  if (enabledOutput.includes("stage=response_error")) {
    throw new Error("enabled SSR diagnostics emitted response_error");
  }
  for (const fragment of [
    "writable_finished=0",
    "writable_finished=1",
    "write_count=2",
    "body_bytes=13",
    "request_id=request-1",
    "cf_ray=ray-1",
  ]) {
    if (!enabledOutput.includes(fragment)) {
      throw new Error(`enabled SSR diagnostics omitted ${fragment}`);
    }
  }

  const disabledOutput = await runFixture("disabled", { disabled: true });
  if (disabledOutput.includes("ssr_stream")
      || !disabledOutput.includes("client_join=none")
      || !disabledOutput.includes("request_start_marker=none")) {
    throw new Error("disabled SSR diagnostics emitted a stream log");
  }

  const untrustedHeadersOutput = await runFixture("public-diagnostic-headers", {
    diagnosticMarker: true,
    sampleRate: "0",
  });
  if (untrustedHeadersOutput.includes("ssr_stream")
      || !untrustedHeadersOutput.includes("client_join=none")) {
    throw new Error("public diagnostic headers promoted an unsampled SSR stream");
  }

  const validatedPlanOutput = await runFixture("validated-plan", {
    disabled: true,
    planSelection: "valid",
  });
  for (const stage of [
    "request_start",
    "response_stream_start",
    "first_body_write_attempt",
    "response_finish",
    "response_close",
  ]) {
    assertCount(validatedPlanOutput, `stage=${stage}`, 1, "validated SSR plan");
  }
  if (!validatedPlanOutput.includes("request_id=00000000000000000000000000000002")
      || !validatedPlanOutput.includes("client_join=00000000000000000000000000000002")
      || !validatedPlanOutput.includes("request_start_marker=set")
      || validatedPlanOutput.includes("session=")
      || validatedPlanOutput.includes("private-slug")) {
    throw new Error("validated plan stream rows lost correlation or exposed private request data");
  }
  const unsampledPlanOutput = await runFixture("validated-plan-unsampled", {
    disabled: true,
    planSelection: "unsampled",
  });
  if (unsampledPlanOutput.includes("ssr_stream")
      || !unsampledPlanOutput.includes("client_join=11111111111111111111111111111111")
      || !unsampledPlanOutput.includes("request_start_marker=set")) {
    throw new Error("unsampled valid typed plan lost its response join or emitted stream rows");
  }
  const closedNoneligibleOutput = await runFixture("closed-noneligible-plan-capacity", {
    disabled: true,
    planSelection: "capacity-close",
  });
  if (!closedNoneligibleOutput.includes(
    "closed_noneligible=128 eligible_after_close=accepted eligible_again=accepted",
  )) {
    throw new Error("closed noneligible plan entries consumed selector capacity");
  }
  const duplicateIdCloseOutput = await runFixture("duplicate-plan-id-close", {
    disabled: true,
    planSelection: "duplicate-id-close",
  });
  assertCount(duplicateIdCloseOutput, "stage=response_stream_start", 2, "duplicate-ID plan close");
  if (!duplicateIdCloseOutput.includes("duplicate_id_replacement=accepted")) {
    throw new Error("closing an older duplicate-ID response removed the newer selection");
  }
  for (const selection of ["wrong-request", "wrong-run", "expired"]) {
    const rejectedPlanOutput = await runFixture(`plan-${selection}`, {
      disabled: true,
      planSelection: selection,
    });
    if (rejectedPlanOutput.includes("ssr_stream")) {
      throw new Error(`SSR stream guard accepted ${selection} selector state`);
    }
    if (!rejectedPlanOutput.includes("client_join=none")) {
      throw new Error(`SSR stream guard set a response join header for ${selection} selector state`);
    }
  }
  const forgedPlanMarkerOutput = await runFixture("forged-plan-marker", {
    disabled: true,
    planSelection: "forged",
  });
  if (forgedPlanMarkerOutput.includes("ssr_stream")) {
    throw new Error("SSR stream guard trusted a caller marker without validated request selection");
  }
  if (!forgedPlanMarkerOutput.includes("client_join=none")) {
    throw new Error("SSR stream guard set a response join header from a caller marker alone");
  }
}

void main().catch((error) => {
  console.error(`SSR diagnostics contract failed: ${error}`);
  process.exitCode = 1;
});
