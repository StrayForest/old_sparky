"use strict";

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

function failedStage(steps) {
  for (const step of steps ?? []) {
    if (step.error && EXPECTED_STEPS.has(step.title)) return step.title;
    const nested = failedStage(step.steps);
    if (nested) return nested;
  }
  return null;
}

class AuthE2ESafeReporter {
  onBegin(_config, suite) {
    const names = suite.allTests().map((test) => test.title);
    if (names.length !== EXPECTED_TESTS.size || names.some((name) => !EXPECTED_TESTS.has(name))) {
      process.stdout.write("AUTH_E2E status=invalid-suite\n");
      throw new Error("Auth E2E suite identity is invalid.");
    }
    this.total = names.length;
    this.passed = 0;
    this.failed = 0;
  }

  onTestEnd(test, result) {
    if (result.status === "passed") {
      this.passed += 1;
    } else {
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
    const status = result.status === "passed" && this.failed === 0 ? "passed" : "failed";
    process.stdout.write(
      `AUTH_E2E status=${status} tests=${this.total ?? 0} passed=${this.passed ?? 0} failed=${this.failed ?? 0}\n`
    );
  }
}

module.exports = AuthE2ESafeReporter;
