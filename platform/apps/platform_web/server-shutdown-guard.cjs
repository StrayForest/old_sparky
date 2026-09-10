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
const ssrRequestStart = Symbol.for("old-sparky.ssr-request-start");
const ssrRequestStartHeader = "x-platform-ssr-request-start-ms";
const timeoutDiagnosticIdHeader = "x-platform-timeout-diagnostic-id";
const timeoutDiagnosticIdPattern = /^tdiag-[0-9]{1,32}-[0-9]{5}$/;
const maxStreamWrites = 100_000;
const maxStreamBytes = 100_000_000;

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
  const diagnosticId = request
    && request.headers
    && request.headers[timeoutDiagnosticIdHeader];
  return safeToken(
    timeoutDiagnosticIdPattern.test(typeof diagnosticId === "string" ? diagnosticId.trim() : "")
      ? diagnosticId
      : "",
    safeToken(request && request.headers && request.headers["x-request-id"], "unknown")
  );
}

function shouldTrace(response) {
  if (!ssrStreamDiagnosticsEnabled) {
    return false;
  }
  const request = requestFor(response);
  const diagnosticId = request
    && request.headers
    && request.headers[timeoutDiagnosticIdHeader];
  const validDiagnosticId = typeof diagnosticId === "string"
    && timeoutDiagnosticIdPattern.test(diagnosticId.trim());
  const sampleKey = request && (
    request.headers["x-request-id"]
    || request.headers["cf-ray"]
    || "unknown"
  );
  if (
    !request
    || request.method !== "GET"
    || (!validDiagnosticId && !sampleRequest(sampleKey, sampleRate()))
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
    logStreamStage(response, "response_error");
  });
}

function streamStateFor(response) {
  if (!shouldTrace(response)) {
    return null;
  }
  if (!response[ssrStreamState]) {
    response[ssrStreamState] = {
      requestStarted: false,
      requestReleased: false,
      responseStarted: false,
      firstBodyWriteAttempt: false,
      finishLogged: false,
      closeLogged: false,
      responseError: false,
      writeCount: 0,
      bodyBytes: 0,
    };
    attachResponseLifecycle(response);
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
  if (!ssrStreamDiagnosticsEnabled || typeof process.getBuiltinModule !== "function") {
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
        request[ssrRequestStart] = Date.now();
        if (request.headers && typeof request.headers === "object") {
          request.headers[ssrRequestStartHeader] = String(request[ssrRequestStart]);
        }
        markRequestStart(response);
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
