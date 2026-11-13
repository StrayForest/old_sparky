"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This CommonJS parser is test support. */

const path = require("node:path");

const SAFE_STAGES = new Set([
  "environment",
  "smtp-capture",
  "application-import",
  "settings-validation",
  "service-isolation",
  "app-creation",
  "route-registration",
  "control-file",
  "uvicorn-startup",
  "preflight",
  "startup"
]);
const SAFE_CATEGORIES = new Set([
  "module-missing",
  "module-import",
  "permission",
  "path-missing",
  "os-error",
  "value-error",
  "type-error",
  "name-error",
  "guard-or-runtime",
  "unexpected"
]);
const SAFE_EXCEPTIONS = new Set([
  "NameError",
  "ModuleNotFoundError",
  "ImportError",
  "RuntimeError",
  "ValueError",
  "TypeError",
  "OSError",
  "PermissionError",
  "FileNotFoundError",
  "AttributeError",
  "AssertionError",
  "SystemExit",
  "TimeoutError",
  "ConnectionError",
  "Exception",
  "ExceptionGroup"
]);
const SAFE_SIGNALS = new Set(["SIGTERM", "SIGKILL", "SIGINT", "SIGHUP"]);

function createPythonDiagnosticParser(root) {
  let buffer = "";
  let inTraceback = false;
  let lastStage = "startup";
  let lastException = "none";
  let lastSource = "unknown";
  let lastLine = 0;

  function sourceAlias(filename) {
    if (!path.isAbsolute(filename)) return "unknown";
    const relative = path.relative(root, path.resolve(filename)).replaceAll("\\", "/");
    if (relative.startsWith("../") || relative === ".." || path.isAbsolute(relative)) {
      return "dependency";
    }
    if (relative.startsWith("tests/support/")
        || relative.startsWith("apps/platform_api/")
        || relative.startsWith("python_packages/platform_infra/")
        || relative.startsWith("python_packages/platform_domain/")) {
      return relative;
    }
    return "dependency";
  }

  function consumeLine(line) {
    const progress = /^AUTH_E2E_API_PROGRESS stage=([a-z-]+)$/u.exec(line);
    if (progress && SAFE_STAGES.has(progress[1])) {
      lastStage = progress[1];
      return "AUTH_E2E_API_PROGRESS stage=" + lastStage + "\n";
    }
    const startup = /^AUTH_E2E_API_STARTUP status=failed stage=([a-z-]+) category=([a-z-]+)$/u
      .exec(line);
    if (startup && SAFE_STAGES.has(startup[1]) && SAFE_CATEGORIES.has(startup[2])) {
      lastStage = startup[1];
      lastException = startup[2];
      return "AUTH_E2E_API_STARTUP status=failed stage=" + lastStage
        + " category=" + startup[2] + "\n";
    }
    if (line === "Traceback (most recent call last):") {
      inTraceback = true;
      lastException = "none";
      lastSource = "unknown";
      lastLine = 0;
      return "";
    }
    if (!inTraceback) return "";
    const frame = /^\s+File "([^"]+)", line ([0-9]{1,6}), in /u.exec(line);
    if (frame) {
      lastSource = sourceAlias(frame[1]);
      lastLine = Number(frame[2]);
      return "";
    }
    const exception = /^([A-Za-z_][A-Za-z0-9_]*)(?::|$)/u.exec(line);
    if (exception) {
      lastException = SAFE_EXCEPTIONS.has(exception[1]) ? exception[1] : "other";
      inTraceback = false;
    }
    return "";
  }

  return {
    consume(chunk) {
      buffer += String(chunk);
      if (buffer.length > 8192) buffer = buffer.slice(-4096);
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      return lines.map(consumeLine).join("");
    },
    exit({ code, signal }) {
      const safeCode = Number.isInteger(code) && code >= 0 && code <= 255
        ? String(code)
        : "none";
      const safeSignal = SAFE_SIGNALS.has(signal) ? signal : signal ? "other" : "none";
      return "AUTH_E2E_API_EXIT status=early stage=" + lastStage
        + " exception=" + lastException + " source=" + lastSource + " line=" + lastLine
        + " code=" + safeCode + " signal=" + safeSignal + "\n";
    }
  };
}

function shouldReportEarlyExit({ stopping, spawnFailed }) {
  return !stopping && !spawnFailed;
}

function shouldInitiateChildStop({ stopping, childExited, exitCode, signalCode }) {
  return !stopping && !childExited && exitCode === null && signalCode === null;
}

module.exports = {
  createPythonDiagnosticParser,
  shouldInitiateChildStop,
  shouldReportEarlyExit
};
