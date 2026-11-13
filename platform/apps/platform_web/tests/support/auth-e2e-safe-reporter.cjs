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
    process.stdout.write(
      `AUTH_E2E_SETUP_ERROR category=${category} component=${component} source=${source} line=${line}\n`
    );
  }

  onStdErr(chunk) {
    const value = String(chunk);
    if (/AUTH_E2E_API_LAUNCH status=(?:missing-config|failed)/u.test(value)) {
      this.observedServerFailure = "api";
      this.observedServerOutputComponent = "api";
    } else if (/AUTH_E2E_NEXT_LAUNCH status=(?:missing-config|failed)/u.test(value)) {
      this.observedServerFailure = "next";
      this.observedServerOutputComponent = "next";
    } else if (/\[auth-e2e-api\]/u.test(value)) {
      this.observedServerOutputComponent = "api";
    } else if (/\[auth-e2e-next\]/u.test(value)) {
      this.observedServerOutputComponent = "next";
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
