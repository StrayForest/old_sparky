"use strict";

const configuredGraceMs = Number.parseInt(
  process.env.PLATFORM_WEB_SHUTDOWN_GRACE_MS ?? "10000",
  10,
);
const graceMs = Number.isFinite(configuredGraceMs)
  ? Math.min(60_000, Math.max(1_000, configuredGraceMs))
  : 10_000;

let shutdownScheduled = false;

const ssrStreamDiagnosticsEnabled = process.env.PLATFORM_SSR_PERF_LOG_ENABLED === "true";
const ssrStreamState = Symbol.for("old-sparky.ssr-stream-state");
const ssrStreamInstallState = Symbol.for("old-sparky.ssr-stream-installed");
const ssrRequestStart = Symbol.for("old-sparky.ssr-request-start");

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

function shouldTrace(response) {
  if (!ssrStreamDiagnosticsEnabled) {
    return false;
  }
  const request = requestFor(response);
  const sampleKey = request && (
    request.headers["x-request-id"]
    || request.headers["cf-ray"]
    || "unknown"
  );
  if (
    !request
    || request.method !== "GET"
    || !sampleRequest(sampleKey, sampleRate())
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
  console.info(
    `ssr_stream request_id=${safeToken(request.headers["x-request-id"], "unknown")}`
      + ` cf_ray=${safeToken(request.headers["cf-ray"], "unknown")}`
      + ` stage=${stage} elapsed_ms=${elapsedSinceRequestStart(request)}`
      + ` status=${Number(response.statusCode) || 0}`
  );
}

function streamStateFor(response) {
  if (!shouldTrace(response)) {
    return null;
  }
  if (!response[ssrStreamState]) {
    response[ssrStreamState] = {
      responseStarted: false,
      firstChunkEmitted: false,
    };
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

function hasBody(chunk) {
  return chunk !== null
    && chunk !== undefined
    && (typeof chunk === "string" ? chunk.length > 0 : chunk.length > 0);
}

function markFirstChunk(response, chunk) {
  const state = streamStateFor(response);
  if (!state || state.firstChunkEmitted || !hasBody(chunk)) {
    return;
  }
  markResponseStreamStart(response);
  state.firstChunkEmitted = true;
  logStreamStage(response, "first_chunk_emitted");
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
    markFirstChunk(this, chunk);
    return originalWrite.call(this, chunk, ...args);
  };

  const originalEnd = responsePrototype.end;
  responsePrototype.end = function (chunk, ...args) {
    markFirstChunk(this, chunk);
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
