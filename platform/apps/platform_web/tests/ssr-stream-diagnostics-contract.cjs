"use strict";

const childProcess = process.getBuiltinModule("node:child_process");
const fs = process.getBuiltinModule("node:fs");
const path = process.getBuiltinModule("node:path");

const guardPath = path.resolve(__dirname, "..", "server-shutdown-guard.cjs");
const fixtureWatchdogMs = 3_000;
const behaviorTimeoutMs = 5_000;
const cleanupTimeoutMs = 1_000;

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

function fixtureSource({ disabled, diagnosticMarker }) {
  return `
const http = process.getBuiltinModule("node:http");
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
      "x-request-id": "${disabled ? "request-disabled" : "request-1"}",
      "cf-ray": "ray-1",
      ${diagnosticMarker ? [
        '"x-platform-timeout-diagnostic-id": "tdiag-123-00001",',
        '"x-platform-ssr-trace": "1",',
        '"x-platform-ssr-request-id": "forged-request-id",',
        '"x-platform-ssr-cf-ray": "forged-cf-ray",',
        '"x-platform-ssr-proxy-start-ms": "1",',
        '"x-platform-ssr-request-start-ms": "1",',
      ].join("\n      ") : ""}
    },
  }).then((response) => response.text()).then(() => {
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

async function runFixture(name, { disabled = false, diagnosticMarker = false, sampleRate = "1" } = {}) {
  const environment = { ...process.env };
  if (disabled) {
    delete environment.PLATFORM_SSR_PERF_LOG_ENABLED;
  } else {
    environment.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
    environment.PLATFORM_SSR_PERF_SAMPLE_RATE = sampleRate;
  }
  const child = childProcess.spawn(
    process.execPath,
    ["--require", guardPath, "-e", fixtureSource({ disabled, diagnosticMarker })],
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
  if (disabledOutput.includes("ssr_stream")) {
    throw new Error("disabled SSR diagnostics emitted a stream log");
  }

  const untrustedHeadersOutput = await runFixture("public-diagnostic-headers", {
    diagnosticMarker: true,
    sampleRate: "0",
  });
  if (untrustedHeadersOutput.includes("ssr_stream")) {
    throw new Error("public diagnostic headers promoted an unsampled SSR stream");
  }
}

void main().catch((error) => {
  console.error(`SSR diagnostics contract failed: ${error}`);
  process.exitCode = 1;
});
