"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This Playwright reporter is CommonJS. */

const path = require("node:path");

const EXPECTED_TESTS = new Set([
  "auth lifecycle uses the real API, local mail capture, and strict verification"
]);
const EXPECTED_STEPS = new Set([
  "01-registration-verification-code",
  "02-login-and-second-session",
  "03-password-reset-and-session-invalidation",
  "04-change-password-and-relogin",
  "05-exact-fixture-cleanup"
]);
const SAFE_ERROR_CODES = new Set([
  "MODULE_NOT_FOUND",
  "ERR_MODULE_NOT_FOUND",
  "ERR_REQUIRE_ESM",
  "ERR_UNKNOWN_FILE_EXTENSION"
]);
const API_STARTUP_PATTERN = new RegExp(
  "^AUTH_E2E_API_STARTUP status=failed stage=("
    + "environment|smtp-capture|application-import|settings-validation|service-isolation"
    + "|app-creation|route-registration|control-file|uvicorn-startup|preflight|startup"
    + ") category=(module-missing|module-import|permission|path-missing|os-error"
    + "|value-error|type-error|name-error|guard-or-runtime|unexpected)$",
  "mu"
);
const API_PROGRESS_PATTERN = /^AUTH_E2E_API_PROGRESS stage=([a-z-]+)$/mu;
const SAFE_API_STAGES = new Set([
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
const API_EXIT_PATTERN = new RegExp(
  "^AUTH_E2E_API_EXIT status=(early|spawn-error) stage=("
    + "launch|environment|smtp-capture|application-import|settings-validation|service-isolation"
    + "|app-creation|route-registration|control-file|uvicorn-startup|preflight|startup"
    + ") exception=(NameError|ModuleNotFoundError|ImportError|RuntimeError|ValueError|TypeError"
    + "|OSError|PermissionError|FileNotFoundError|AttributeError|AssertionError|SystemExit"
    + "|TimeoutError|ConnectionError|Exception|ExceptionGroup|other|none)"
    + " source=(tests/support/[A-Za-z0-9_./-]+|apps/platform_api/[A-Za-z0-9_./-]+"
    + "|python_packages/platform_infra/[A-Za-z0-9_./-]+"
    + "|python_packages/platform_domain/[A-Za-z0-9_./-]+|dependency|none|unknown)"
    + " line=([0-9]{1,6}) code=(none|[0-9]{1,3})"
    + " signal=(none|SIGTERM|SIGKILL|SIGINT|SIGHUP|other)$",
  "mu"
);

function errorCategory(error) {
  const message = typeof error?.message === "string" ? error.message : "";
  const code = typeof error?.code === "string" ? error.code : "";
  if (/no tests found/iu.test(message)) return "no-tests";
  if (/webserver|web server|server process/iu.test(message)) return "webserver-setup";
  if (/global setup/iu.test(message)) return "global-setup";
  if (SAFE_ERROR_CODES.has(code) || error?.location
      || /syntaxerror|failed to load|cannot find module|transformerror/iu.test(message)) {
    return "module-load";
  }
  return "setup";
}

function errorLocation(error) {
  const location = error?.location;
  if (!location || typeof location.file !== "string") {
    return { source: "unknown", line: 0 };
  }
  const absolute = path.isAbsolute(location.file)
    ? path.normalize(location.file)
    : path.resolve(process.cwd(), location.file);
  const source = path.relative(process.cwd(), absolute).replaceAll("\\", "/");
  const allowed = source.startsWith("tests/")
    || source === "playwright.auth-lifecycle-e2e.config.ts";
  const line = Number.isSafeInteger(location.line) && location.line > 0
    ? location.line
    : 0;
  return allowed ? { source, line } : { source: "unknown", line: 0 };
}

function serverComponent(error, observedFailure, observedOutput) {
  const message = typeof error?.message === "string" ? error.message : "";
  if (message.includes(":3199")) return "api";
  if (message.includes(":3100")) return "next";
  if (observedFailure === "api" || observedFailure === "next") return observedFailure;
  if (observedOutput === "api" || observedOutput === "next") return observedOutput;
  return "unknown";
}

function webServerFailure(error) {
  const message = typeof error?.message === "string" ? error.message : "";
  if (/is already used|already listening/iu.test(message)) {
    return { branch: "already-in-use", exitCode: "none" };
  }
  const exit = /Process from config\.webServer was not able to start\. Exit code: ([0-9]{1,3})/u
    .exec(message);
  if (exit) {
    const code = Number(exit[1]);
    return { branch: "spawn-exit", exitCode: code <= 255 ? String(code) : "none" };
  }
  if (/Process from config\.webServer exited early/iu.test(message)) {
    const earlyExit = /Exit code: ([0-9]{1,3})/u.exec(message);
    const code = earlyExit ? Number(earlyExit[1]) : null;
    return {
      branch: "early-exit",
      exitCode: code !== null && code <= 255 ? String(code) : "none"
    };
  }
  if (/timed out|timeout/iu.test(message)) {
    return { branch: "timeout", exitCode: "none" };
  }
  return { branch: "unknown", exitCode: "none" };
}

function safeApiSource(source) {
  if (["dependency", "none", "unknown"].includes(source)) return source;
  const parts = source.split("/");
  if (parts.some((part) => part === "." || part === "..")) return "unknown";
  if (source.startsWith("tests/support/")
      || source.startsWith("apps/platform_api/")
      || source.startsWith("python_packages/platform_infra/")
      || source.startsWith("python_packages/platform_domain/")) {
    return source;
  }
  return "unknown";
}

function isExpectedTest(test) {
  const file = typeof test?.location?.file === "string" ? test.location.file : "";
  const absolute = path.isAbsolute(file) ? path.normalize(file) : path.resolve(process.cwd(), file);
  const source = path.relative(process.cwd(), absolute).replaceAll("\\", "/");
  return EXPECTED_TESTS.has(test?.titlePath?.().at(-1) ?? "")
    && source === "tests/smoke/auth-lifecycle-e2e.spec.ts";
}

function failedStage(steps) {
  for (const step of steps ?? []) {
    if (step.error && EXPECTED_STEPS.has(step.title)) return step.title;
    const nested = failedStage(step.steps);
    if (nested) return nested;
  }
  return null;
}

class AuthE2ESafeReporter {
  constructor() {
    this.discoveryRequested = process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY === "1";
    this.listMode = process.argv.includes("--list");
    this.discoveryProtocolValid = this.discoveryRequested === this.listMode;
    this.reportedGlobalErrors = 0;
    this.observedServerFailure = "unknown";
    this.observedServerOutputComponent = "unknown";
    this.serverStderrBuffer = "";
    this.globalError = false;
    this.executedBegins = 0;
    this.executedAttempts = 0;
  }

  onBegin(_config, suite) {
    const tests = suite.allTests();
    const exact = tests.filter(isExpectedTest);
    this.total = tests.length;
    this.exact = exact.length;
    this.invalidSuite = tests.length !== EXPECTED_TESTS.size
      || exact.length !== EXPECTED_TESTS.size;
    this.passed = 0;
    this.failed = 0;

    if (!this.discoveryProtocolValid) {
      this.invalidSuite = true;
      process.stdout.write("AUTH_E2E status=invalid-discovery-mode\n");
      throw new Error("Auth E2E discovery mode does not match the runner mode.");
    }
    if (this.invalidSuite) {
      process.stdout.write(`AUTH_E2E status=invalid-suite tests=${tests.length} exact=${exact.length}\n`);
      // Playwright v1.61 may synthesize an empty onBegin after an earlier
      // load/setup error. Preserve that error for the closed onError reporter.
      if (tests.length > 0) throw new Error("Auth E2E suite identity is invalid.");
      return;
    }
    if (this.discoveryRequested) {
      process.stdout.write(`AUTH_E2E_DISCOVERY status=collected tests=${tests.length} exact=${exact.length}\n`);
    }
  }

  onError(error) {
    this.globalError = true;
    if (this.reportedGlobalErrors >= 4) return;
    this.reportedGlobalErrors += 1;
    const { source, line } = errorLocation(error);
    const category = errorCategory(error);
    const component = category === "webserver-setup"
      ? serverComponent(error, this.observedServerFailure, this.observedServerOutputComponent)
      : "none";
    if (category === "webserver-setup") {
      const failure = webServerFailure(error);
      process.stdout.write(
        "AUTH_E2E_WEB_SERVER component=" + component
        + " branch=" + failure.branch + " exit=" + failure.exitCode + "\n"
      );
    }
    process.stdout.write(
      `AUTH_E2E_SETUP_ERROR category=${category} component=${component} source=${source} line=${line}\n`
    );
  }

  onStdErr(chunk) {
    const value = String(chunk);
    const buffered = this.serverStderrBuffer + value;
    const lines = buffered.split("\n");
    this.serverStderrBuffer = lines.pop() ?? "";
    if (this.serverStderrBuffer.length > 1024) {
      this.serverStderrBuffer = this.serverStderrBuffer.slice(-1024);
    }
    const normalized = lines.map((line) => line
      .replace(/\u001b\[[0-9;]*m/gu, "")
      .replace(/^\[auth-e2e-api\]\s*/u, ""));
    const safeValue = normalized.join("\n");
    const progress = API_PROGRESS_PATTERN.exec(safeValue);
    if (progress && SAFE_API_STAGES.has(progress[1])) {
      this.observedServerFailure = "api";
      this.observedServerOutputComponent = "api";
      process.stdout.write("AUTH_E2E_API_PROGRESS stage=" + progress[1] + "\n");
    }
    const apiStartup = API_STARTUP_PATTERN.exec(safeValue);
    if (apiStartup) {
      this.observedServerFailure = "api";
      this.observedServerOutputComponent = "api";
      if (!this.reportedApiStartup) {
        this.reportedApiStartup = true;
        process.stdout.write(
          "AUTH_E2E_API_STARTUP stage=" + apiStartup[1] + " category=" + apiStartup[2] + "\n"
        );
      }
    } else if (/AUTH_E2E_API_LAUNCH status=(?:missing-config|failed)/u.test(safeValue)) {
      this.observedServerFailure = "api";
      this.observedServerOutputComponent = "api";
    } else if (/AUTH_E2E_NEXT_LAUNCH status=(?:missing-config|failed)/u.test(safeValue)) {
      this.observedServerFailure = "next";
      this.observedServerOutputComponent = "next";
    } else if (/\[auth-e2e-api\]/u.test(value)) {
      this.observedServerOutputComponent = "api";
    } else if (/\[auth-e2e-next\]/u.test(value)) {
      this.observedServerOutputComponent = "next";
    }
    const apiExit = API_EXIT_PATTERN.exec(safeValue);
    if (apiExit) {
      this.observedServerFailure = "api";
      this.observedServerOutputComponent = "api";
      const rawCode = apiExit[6] === "none" ? "none" : Number(apiExit[6]);
      const code = rawCode === "none" || rawCode <= 255 ? String(rawCode) : "none";
      process.stdout.write(
        "AUTH_E2E_API_EXIT status=" + apiExit[1] + " stage=" + apiExit[2]
        + " exception=" + apiExit[3] + " source=" + safeApiSource(apiExit[4])
        + " line=" + apiExit[5] + " code=" + code + " signal=" + apiExit[7] + "\n"
      );
    }
  }

  onTestBegin(test) {
    this.executedBegins += 1;
    if (!isExpectedTest(test)) this.invalidSuite = true;
  }

  onTestEnd(test, result) {
    this.executedAttempts += 1;
    const validAttempt = isExpectedTest(test) && result.retry === 0;
    if (result.status === "passed" && validAttempt) {
      this.passed += 1;
    } else {
      this.invalidSuite = true;
      this.failed += 1;
      const stage = failedStage(result.steps) ?? "unattributed";
      const category = result.status === "timedOut"
        ? "timeout"
        : stage === "unattributed" ? "setup" : "assertion";
      const source = stage === "unattributed"
        ? "tests/smoke/auth-lifecycle-e2e.spec.ts#test"
        : `tests/smoke/auth-lifecycle-e2e.spec.ts#${stage}`;
      process.stdout.write(
        `AUTH_E2E_FAILURE test=auth-lifecycle stage=${stage} category=${category} source=${source} line=${test.location.line}\n`
      );
    }
  }

  onEnd(result) {
    if (!this.discoveryProtocolValid) {
      process.stdout.write(
        `AUTH_E2E status=failed tests=${this.total ?? 0} passed=0 failed=0\n`
      );
      return { status: "failed" };
    }
    if (this.discoveryRequested) {
      const status = result.status === "passed" && !this.invalidSuite && !this.globalError
          && this.total === 1 && this.exact === 1
          && this.executedBegins === 0 && this.executedAttempts === 0
        ? "passed"
        : "failed";
      process.stdout.write(
        `AUTH_E2E_DISCOVERY status=${status} tests=${this.total ?? 0} exact=${this.exact ?? 0}\n`
      );
      return status === "passed" ? undefined : { status: "failed" };
    }
    if (this.invalidSuite || this.globalError || result.status !== "passed"
        || this.total !== 1 || this.exact !== 1
        || this.executedBegins !== 1 || this.executedAttempts !== 1
        || this.passed !== 1 || this.failed !== 0) {
      process.stdout.write(
        `AUTH_E2E status=failed tests=${this.total ?? 0} passed=${this.passed ?? 0} failed=${this.failed ?? 0} attempts=${this.executedAttempts ?? 0} errors=${this.globalError ? 1 : 0}\n`
      );
      return { status: "failed" };
    }
    const status = result.status === "passed" && this.failed === 0 ? "passed" : "failed";
    process.stdout.write(
      `AUTH_E2E status=${status} tests=${this.total ?? 0} passed=${this.passed ?? 0} failed=${this.failed ?? 0}\n`
    );
  }
}

module.exports = AuthE2ESafeReporter;
