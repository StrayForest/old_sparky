"use strict";

const configuredGraceMs = Number.parseInt(
  process.env.PLATFORM_WEB_SHUTDOWN_GRACE_MS ?? "10000",
  10,
);
const graceMs = Number.isFinite(configuredGraceMs)
  ? Math.min(60_000, Math.max(1_000, configuredGraceMs))
  : 10_000;

let shutdownScheduled = false;
let activeSsrRequests = 0;

const ssrStreamDiagnosticsEnabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
const ssrStreamState = Symbol.for("old-sparky.ssr-stream-state");
const ssrStreamInstallState = Symbol.for("old-sparky.ssr-stream-installed");
const ssrStreamSelector = Symbol.for("old-sparky.ssr-stream-selector");
const ssrRequestStart = Symbol.for("old-sparky.ssr-request-start");
const ssrRequestStartHeader = "x-platform-ssr-request-start-ms";
const ssrTraceHeader = "x-platform-ssr-trace";
const maxSelectedRequests = 128;
const maxPendingRequests = 128;
const maxPlanDurationMs = 160_000;
const maxRowsPerRequest = 6;
const maxRowsPerPlan = 6_000;
const maxSelectorCoverageRuns = 32;
const captureSampleScheme = "sha256-runid-requestid-1of8-v1";
const maxCaptureCoverageRuns = 32;
const maxStreamWrites = 100_000;
const maxStreamBytes = 100_000_000;
const selectedPlanRequests = new Map();
const pendingPlanRequests = new Map();
const planRows = new Map();
const captureCoverageByRun = new Map();
const selectorOverflowByRun = new Map();
const selectorCoverageReported = new Map();
let selectorOverflowUnattributed = 0;

function isCaptureEligible(runId, requestId) {
  if (!/^[0-9a-f]{32}$/.test(runId || "")
      || !/^[0-9a-f]{32}$/.test(requestId || "")
      || typeof process.getBuiltinModule !== "function") return false;
  const crypto = process.getBuiltinModule("node:crypto");
  if (!crypto || typeof crypto.createHash !== "function") return false;
  const digest = crypto.createHash("sha256")
    .update(`${runId}:${requestId}`, "ascii")
    .digest();
  return digest.length === 32 && (digest[0] & 7) === 0;
}

function reportCaptureCoverage(runId) {
  const coverage = captureCoverageByRun.get(runId);
  if (!coverage || coverage.reported) return;
  coverage.reported = true;
  if (coverage.timer) clearTimeout(coverage.timer);
  console.info(
    `ssr_stream_capture_coverage scheme=${captureSampleScheme} run_id=${runId}`
      + ` eligible=${coverage.eligibleRequests}`
      + ` retained=${coverage.retainedRequests}`
      + ` lost=${coverage.lostRequests}`
      + ` rows=${coverage.rows}`
      + ` lost_rows=${coverage.lostRows}`
      + ` status=${coverage.truncated ? "truncated" : "complete"}`,
  );
  captureCoverageByRun.delete(runId);
}

function captureCoverageFor(runId, expiresAtMs) {
  let coverage = captureCoverageByRun.get(runId);
  if (coverage) return coverage;
  if (captureCoverageByRun.size >= maxCaptureCoverageRuns) return null;
  coverage = {
    eligibleRequests: 0,
    retainedRequests: 0,
    lostRequests: 0,
    rows: 0,
    lostRows: 0,
    truncated: false,
    reported: false,
    timer: null,
  };
  coverage.timer = setTimeout(
    () => reportCaptureCoverage(runId),
    Math.max(1, expiresAtMs - Date.now()),
  );
  coverage.timer.unref?.();
  captureCoverageByRun.set(runId, coverage);
  return coverage;
}

function sampleRate() {
  const value = Number(process.env.PLATFORM_SSR_PERF_SAMPLE_RATE);
  return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0.01;
}

function sampleRequest(requestId, rate) {
  let hash = 2166136261;
  for (const character of requestId) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash / 0x1_0000_0000 < rate;
}

function safeToken(value, fallback) {
  const normalized = typeof value === "string" ? value.trim() : "";
  return /^[A-Za-z0-9._:-]{1,128}$/.test(normalized) ? normalized : fallback;
}

function requestFor(response) {
  return response && response.req && typeof response.req === "object"
    ? response.req
    : null;
}

function requestCorrelationId(request) {
  return safeToken(request && request.headers && request.headers["x-request-id"], "unknown");
}

function prunePlanState(now = Date.now()) {
  for (const [requestId, selection] of selectedPlanRequests) {
    if (selection.expiresAtMs <= now) selectedPlanRequests.delete(requestId);
  }
  for (const [requestId, pending] of pendingPlanRequests) {
    if (pending.expiresAtMs <= now) pendingPlanRequests.delete(requestId);
  }
  for (const [runId, rows] of planRows) {
    if (rows.expiresAtMs <= now) {
      reportCaptureCoverage(runId);
      planRows.delete(runId);
    }
  }
  for (const [runId, expiresAtMs] of selectorCoverageReported) {
    if (expiresAtMs <= now) selectorCoverageReported.delete(runId);
  }
  for (const [runId, overflow] of selectorOverflowByRun) {
    if (overflow.expiresAtMs <= now) selectorOverflowByRun.delete(runId);
  }
}

function noteSelectorOverflow(runId) {
  if (!/^[0-9a-f]{32}$/.test(runId || "")) return;
  if (!selectorOverflowByRun.has(runId)
      && selectorOverflowByRun.size >= maxSelectorCoverageRuns) {
    selectorOverflowUnattributed = Math.min(1_000_000, selectorOverflowUnattributed + 1);
    return;
  }
  const previous = selectorOverflowByRun.get(runId);
  selectorOverflowByRun.set(runId, {
    count: Math.min(1_000_000, (previous?.count ?? 0) + 1),
    expiresAtMs: Date.now() + maxPlanDurationMs,
  });
}

function reportSelectorCoverage(runId, expiresAtMs) {
  const overflow = selectorOverflowByRun.get(runId);
  if ((!overflow || overflow.count < 1) && selectorOverflowUnattributed < 1) return;
  if (selectorCoverageReported.has(runId)) return;
  if (selectorCoverageReported.size >= maxSelectorCoverageRuns) return;
  selectorOverflowByRun.delete(runId);
  selectorCoverageReported.set(runId, expiresAtMs);
  console.info(
    `ssr_stream_coverage run_id=${runId}`
      + ` selector_overflow=${overflow?.count ?? 0}`
      + ` selector_overflow_unattributed=${selectorOverflowUnattributed} status=truncated`,
  );
  selectorOverflowUnattributed = 0;
}

function selectedPlanFor(request) {
  const requestId = requestCorrelationId(request);
  if (requestId === "unknown" || !request || !request.headers) return null;
  const selection = selectedPlanRequests.get(requestId);
  if (!selection || selection.expiresAtMs <= Date.now()
      || request.headers[ssrTraceHeader] !== selection.runId) return null;
  return selection;
}

function registerValidatedPlanRequest(entry) {
  const now = Date.now();
  prunePlanState(now);
  const pending = entry && pendingPlanRequests.get(entry.requestId);
  if (!entry || typeof entry !== "object"
      || !/^[0-9a-f]{32}$/.test(entry.requestId)
      || !/^[0-9a-f]{32}$/.test(entry.runId)
      || typeof entry.captureEligible !== "boolean"
      || !Number.isSafeInteger(entry.expiresAtMs)
      || entry.expiresAtMs <= now
      || entry.expiresAtMs - now > maxPlanDurationMs) return false;
  const expectedCaptureEligibility = isCaptureEligible(entry.runId, entry.requestId);
  if (entry.captureEligible !== expectedCaptureEligibility) return false;
  const coverage = captureCoverageFor(entry.runId, entry.expiresAtMs);
  if (coverage && entry.captureEligible) {
    coverage.eligibleRequests = Math.min(1_000_000, coverage.eligibleRequests + 1);
  }
  if (!pending
      || pending.request.headers["x-request-id"] !== entry.requestId
      || pending.request.headers[ssrTraceHeader] !== entry.runId) {
    if (coverage && entry.captureEligible) {
      coverage.lostRequests = Math.min(1_000_000, coverage.lostRequests + 1);
      coverage.truncated = true;
    }
    reportSelectorCoverage(entry.runId, entry.expiresAtMs);
    return false;
  }
  if (!selectedPlanRequests.has(entry.requestId)
      && selectedPlanRequests.size >= maxSelectedRequests) {
    if (coverage && entry.captureEligible) {
      coverage.lostRequests = Math.min(1_000_000, coverage.lostRequests + 1);
      coverage.truncated = true;
    }
    noteSelectorOverflow(entry.runId);
    reportSelectorCoverage(entry.runId, entry.expiresAtMs);
    return false;
  }
  const selection = {
    runId: entry.runId,
    expiresAtMs: entry.expiresAtMs,
    captureEligible: entry.captureEligible,
  };
  selectedPlanRequests.set(entry.requestId, selection);
  pending.response.once("close", () => {
    if (selectedPlanRequests.get(entry.requestId) === selection) {
      selectedPlanRequests.delete(entry.requestId);
    }
  });
  if (coverage && entry.captureEligible) {
    coverage.retainedRequests = Math.min(1_000_000, coverage.retainedRequests + 1);
  }
  if (!pending.response.headersSent && typeof pending.response.setHeader === "function") {
    pending.response.setHeader("x-platform-ssr-request-id", entry.requestId);
  }
  reportSelectorCoverage(entry.runId, entry.expiresAtMs);
  pendingPlanRequests.delete(entry.requestId);
  markRequestStart(pending.response);
  return true;
}

if (typeof globalThis[ssrStreamSelector] !== "function") {
  globalThis[ssrStreamSelector] = registerValidatedPlanRequest;
}

function streamPlanSelection(response) {
  if (selectedPlanRequests.size === 0) return null;
  return selectedPlanFor(requestFor(response));
}

function shouldTrace(response) {
  if (!ssrStreamDiagnosticsEnabled && selectedPlanRequests.size === 0) return false;
  const request = requestFor(response);
  const planSelection = streamPlanSelection(response);
  if (planSelection && !planSelection.captureEligible) return false;
  const envSelected = ssrStreamDiagnosticsEnabled;
  if (!envSelected && !planSelection) return false;
  const sampleKey = request && (
    request.headers["x-request-id"]
    || request.headers["cf-ray"]
    || "unknown"
  );
  if (
    !request
    || request.method !== "GET"
    || (envSelected && !planSelection && !sampleRequest(sampleKey, sampleRate()))
  ) {
    return false;
  }
  const pathname = String(request.url || "").split("?", 1)[0];
  return /^\/tournaments\/[^/]+$/.test(pathname)
    && String(request.headers.accept || "").toLowerCase().includes("text/html");
}

function elapsedSinceRequestStart(request) {
  const startedAt = Number(request[ssrRequestStart]);
  if (!Number.isSafeInteger(startedAt) || startedAt <= 0) {
    return 0;
  }
  return Math.max(0, Date.now() - startedAt);
}

function logStreamStage(response, stage) {
  const request = requestFor(response);
  if (!request) {
    return;
  }
  const state = response[ssrStreamState];
  if (state?.planRunId !== null && state?.planExpiresAtMs <= Date.now()) return;
  if (state && state.logCount >= maxRowsPerRequest) {
    if (state.planRunId !== null) {
      const coverage = captureCoverageByRun.get(state.planRunId);
      if (coverage) {
        coverage.lostRows = Math.min(1_000_000, coverage.lostRows + 1);
        coverage.truncated = true;
      }
    }
    return;
  }
  if (state && state.planRunId !== null) {
    prunePlanState();
    const planState = planRows.get(state.planRunId) || {
      rows: 0,
      truncated: false,
      expiresAtMs: state.planExpiresAtMs,
    };
    if (planState.rows >= maxRowsPerPlan) {
      planState.truncated = true;
      const coverage = captureCoverageByRun.get(state.planRunId);
      if (coverage) {
        coverage.lostRows = Math.min(1_000_000, coverage.lostRows + 1);
        coverage.truncated = true;
      }
      planRows.set(state.planRunId, planState);
      return;
    }
    planState.rows += 1;
    planRows.set(state.planRunId, planState);
    const coverage = captureCoverageByRun.get(state.planRunId);
    if (coverage) coverage.rows = Math.min(1_000_000, coverage.rows + 1);
  }
  console.info(
    `ssr_stream request_id=${requestCorrelationId(request)}`
      + ` cf_ray=${safeToken(request.headers["cf-ray"], "unknown")}`
      + ` stage=${stage} elapsed_ms=${elapsedSinceRequestStart(request)}`
      + ` active_requests=${activeSsrRequests}`
      + ` status=${Number(response.statusCode) || 0}`
      + ` writable_finished=${response.writableFinished ? 1 : 0}`
      + ` write_count=${state ? state.writeCount : 0}`
      + ` body_bytes=${state ? state.bodyBytes : 0}`
      + ` response_error=${state && state.responseError ? 1 : 0}`
  );
  if (state) state.logCount += 1;
}

function markRequestStart(response) {
  const state = streamStateFor(response);
  if (!state || state.requestStarted) {
    return;
  }
  state.requestStarted = true;
  activeSsrRequests += 1;
  logStreamStage(response, "request_start");
}

function releaseRequest(response) {
  const state = response[ssrStreamState];
  if (!state || state.requestReleased) {
    return;
  }
  state.requestReleased = true;
  activeSsrRequests = Math.max(0, activeSsrRequests - 1);
}

function attachResponseLifecycle(response) {
  response.once("finish", () => {
    const state = response[ssrStreamState];
    if (!state || state.finishLogged) {
      return;
    }
    state.finishLogged = true;
    logStreamStage(response, "response_finish");
  });
  response.once("close", () => {
    const state = response[ssrStreamState];
    if (!state || state.closeLogged) {
      return;
    }
    state.closeLogged = true;
    logStreamStage(response, "response_close");
    releaseRequest(response);
  });
  response.once("error", () => {
    const state = response[ssrStreamState];
    if (!state) {
      return;
    }
    state.responseError = true;
    if (state.responseErrorLogged) return;
    state.responseErrorLogged = true;
    logStreamStage(response, "response_error");
  });
}

function streamStateFor(response) {
  if (!ssrStreamDiagnosticsEnabled && selectedPlanRequests.size === 0) return null;
  const existingState = response[ssrStreamState];
  if (existingState?.planRunId !== null && existingState?.planExpiresAtMs <= Date.now()) return null;
  if (!shouldTrace(response)) {
    return null;
  }
  const selection = streamPlanSelection(response);
  if (!response[ssrStreamState]) {
    response[ssrStreamState] = {
      requestStarted: false,
      requestReleased: false,
      responseStarted: false,
      firstBodyWriteAttempt: false,
      finishLogged: false,
      closeLogged: false,
      responseError: false,
      responseErrorLogged: false,
      writeCount: 0,
      bodyBytes: 0,
      logCount: 0,
      planRunId: selection?.runId ?? null,
      planExpiresAtMs: selection?.expiresAtMs ?? 0,
    };
    attachResponseLifecycle(response);
    if (requestFor(response)?.[ssrRequestStart]) markRequestStart(response);
  } else if (selection && response[ssrStreamState].planRunId === null) {
    response[ssrStreamState].planRunId = selection.runId;
    response[ssrStreamState].planExpiresAtMs = selection.expiresAtMs;
  }
  const request = requestFor(response);
  const traceMarker = request?.headers?.[ssrTraceHeader];
  const diagnosticCandidate = typeof traceMarker === "string"
    && /^[0-9a-f]{32}$/.test(traceMarker);
  if (diagnosticCandidate
      && ssrStreamDiagnosticsEnabled
      && !selection
      && response[ssrStreamState]
      && !response[ssrStreamState].requestStarted) {
    markRequestStart(response);
  }
  return response[ssrStreamState];
}

function markResponseStreamStart(response) {
  const state = streamStateFor(response);
  if (!state || state.responseStarted) {
    return;
  }
  state.responseStarted = true;
  logStreamStage(response, "response_stream_start");
}

function chunkBytes(chunk) {
  if (chunk === null || chunk === undefined) {
    return 0;
  }
  if (typeof chunk === "string") {
    return Math.min(maxStreamBytes, Buffer.byteLength(chunk));
  }
  const byteLength = Number(chunk.byteLength ?? chunk.length ?? 0);
  return Number.isFinite(byteLength)
    ? Math.min(maxStreamBytes, Math.max(0, byteLength))
    : 0;
}

function markBodyWriteAttempt(response, chunk) {
  const state = streamStateFor(response);
  if (!state) {
    return;
  }
  const bytes = chunkBytes(chunk);
  state.writeCount = Math.min(maxStreamWrites, state.writeCount + 1);
  state.bodyBytes = Math.min(maxStreamBytes, state.bodyBytes + bytes);
  if (state.firstBodyWriteAttempt || bytes === 0) {
    return;
  }
  markResponseStreamStart(response);
  state.firstBodyWriteAttempt = true;
  logStreamStage(response, "first_body_write_attempt");
}

function installSsrStreamDiagnostics() {
  if (typeof process.getBuiltinModule !== "function") {
    return;
  }
  const http = process.getBuiltinModule("node:http");
  const responsePrototype = http && http.ServerResponse && http.ServerResponse.prototype;
  if (!responsePrototype || responsePrototype[ssrStreamInstallState]) {
    return;
  }
  responsePrototype[ssrStreamInstallState] = true;

  const serverPrototype = http.Server && http.Server.prototype;
  if (serverPrototype && !serverPrototype[ssrStreamInstallState]) {
    serverPrototype[ssrStreamInstallState] = true;
    const originalEmit = serverPrototype.emit;
    serverPrototype.emit = function (event, request, response, ...args) {
      if (event === "request" && request && typeof request === "object") {
        const traceMarker = request.headers && request.headers[ssrTraceHeader];
        const diagnosticCandidate = typeof traceMarker === "string"
          && traceMarker.length === 32 && /^[0-9a-f]{32}$/.test(traceMarker);
        if (ssrStreamDiagnosticsEnabled || diagnosticCandidate) {
          request[ssrRequestStart] = Date.now();
          if (request.headers && typeof request.headers === "object") {
            request.headers[ssrRequestStartHeader] = String(request[ssrRequestStart]);
          }
        }
        if (ssrStreamDiagnosticsEnabled && !diagnosticCandidate) {
          markRequestStart(response);
        }
        if (diagnosticCandidate) {
          const requestId = requestCorrelationId(request);
          prunePlanState();
          const selection = selectedPlanRequests.get(requestId);
          if (selection && selection.runId === traceMarker) {
            if (!ssrStreamDiagnosticsEnabled) markRequestStart(response);
          } else if (/^[0-9a-f]{32}$/.test(requestId)) {
            if (pendingPlanRequests.size < maxPendingRequests) {
              const pending = {
                request,
                response,
                expiresAtMs: Date.now() + maxPlanDurationMs,
              };
              pendingPlanRequests.set(requestId, pending);
              response.once("close", () => {
                if (pendingPlanRequests.get(requestId) === pending) {
                  pendingPlanRequests.delete(requestId);
                }
              });
            } else {
              noteSelectorOverflow(traceMarker);
            }
          }
        }
      }
      return originalEmit.call(this, event, request, response, ...args);
    };
  }

  const originalWriteHead = responsePrototype.writeHead;
  responsePrototype.writeHead = function (...args) {
    markResponseStreamStart(this);
    return originalWriteHead.apply(this, args);
  };

  const originalWrite = responsePrototype.write;
  responsePrototype.write = function (chunk, ...args) {
    markBodyWriteAttempt(this, chunk);
    return originalWrite.call(this, chunk, ...args);
  };

  const originalEnd = responsePrototype.end;
  responsePrototype.end = function (chunk, ...args) {
    markBodyWriteAttempt(this, chunk);
    return originalEnd.call(this, chunk, ...args);
  };
}

installSsrStreamDiagnostics();

for (const [signal, exitCode] of [["SIGINT", 130], ["SIGTERM", 143]]) {
  process.once(signal, () => {
    if (shutdownScheduled) {
      return;
    }
    shutdownScheduled = true;
    const forceExit = setTimeout(() => {
      console.log(`Web shutdown grace period (${graceMs} ms) elapsed; exiting.`);
      process.exit(exitCode);
    }, graceMs);
    forceExit.unref();
  });
}
