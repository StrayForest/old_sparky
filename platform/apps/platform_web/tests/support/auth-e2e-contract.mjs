import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const platformRoot = path.resolve(webRoot, "../..");
const repoRoot = path.resolve(platformRoot, "..");
const read = (relative) => readFileSync(path.join(repoRoot, relative), "utf8");

const apiLauncher = read("platform/apps/platform_web/tests/support/auth-e2e-api-launcher.cjs");
const nextLauncher = read("platform/apps/platform_web/tests/support/auth-e2e-next-launcher.cjs");
const config = read("platform/apps/platform_web/playwright.auth-lifecycle-e2e.config.ts");
const genericConfig = read("platform/apps/platform_web/playwright.config.ts");
const liveConfig = read("platform/apps/platform_web/playwright.live.config.ts");
const reporter = read("platform/apps/platform_web/tests/support/auth-e2e-safe-reporter.cjs");
const spec = read("platform/apps/platform_web/tests/smoke/auth-lifecycle-e2e.spec.ts");
const server = read("platform/tests/support/auth_lifecycle_e2e_server.py");
const hermetic = read("platform/tools/platform_web_hermetic.sh");
const childEnvSource = read("platform/apps/platform_web/tests/support/auth-e2e-child-env.cjs");
const diagnosticsSource = read("platform/apps/platform_web/tests/support/auth-e2e-python-diagnostics.cjs");

const require = createRequire(import.meta.url);
const { apiChildEnv, nextChildEnv } = require("./auth-e2e-child-env.cjs");
const AuthE2ESafeReporter = require("./auth-e2e-safe-reporter.cjs");
const {
  createPythonDiagnosticParser,
  shouldInitiateChildStop,
  shouldReportEarlyExit
} =
  require("./auth-e2e-python-diagnostics.cjs");
const parentOnly = {
  PATH: process.env.PATH || "/usr/bin:/bin",
  CI: "true",
  GITHUB_ACTIONS: "true",
  AUTH_E2E_PARENT_ONLY_SENTINEL: "parent-secret-sentinel",
  GITHUB_TOKEN: "parent-token-sentinel",
  PLATFORM_AUTH_E2E: "1",
  PLATFORM_AUTH_E2E_CONTROL_FILE: "/tmp/private/control",
  PLATFORM_ENV_FILE: "/dev/null",
  PLATFORM_ENVIRONMENT: "test",
  PLATFORM_DATABASE_URL: "postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/platformdb_test",
  PLATFORM_DB_SCHEMA: "platform",
  PLATFORM_SECRET_KEY: "ci-only-auth-e2e-secret-key-with-more-than-32-chars",
  PLATFORM_REDIS_URL: "redis://127.0.0.1:6379/15",
  PLATFORM_CELERY_BROKER_URL: "redis://127.0.0.1:6379/13",
  PLATFORM_CELERY_RESULT_BACKEND: "redis://127.0.0.1:6379/14",
  PLATFORM_WEB_ORIGIN: "http://127.0.0.1:3100",
  PLATFORM_API_HOST: "127.0.0.1",
  PLATFORM_API_PORT: "3199",
  PLATFORM_PUBLIC_REGISTRATION_ENABLED: "true",
  PLATFORM_EMAIL_VERIFICATION_REQUIRED: "true",
  PLATFORM_TURNSTILE_MODE: "always",
  PLATFORM_TURNSTILE_SITE_KEY: "auth-e2e-public-site-key",
  PLATFORM_TURNSTILE_SECRET_KEY: "auth-e2e-not-a-provider-secret",
  PLATFORM_TURNSTILE_EXPECTED_HOSTNAME: "127.0.0.1",
  PLATFORM_AUTH_GENERIC_RESPONSE_MIN_SECONDS: "0",
  PLATFORM_UPLOAD_DIR: "/tmp/auth-e2e-uploads",
  PLATFORM_MEDIA_STAGING_DIR: "/tmp/auth-e2e-media",
  PLATFORM_RESEND_API_KEY: "must-not-forward",
  PLATFORM_SUPPORT_SMTP_PASSWORD: "must-not-forward",
  PLATFORM_GOOGLE_CLIENT_SECRET: "must-not-forward"
};
const forbiddenChildNames = [
  "AUTH_E2E_PARENT_ONLY_SENTINEL",
  "GITHUB_TOKEN",
  "PLATFORM_RESEND_API_KEY",
  "PLATFORM_SUPPORT_SMTP_PASSWORD",
  "PLATFORM_GOOGLE_CLIENT_SECRET"
];
const probeSource = `const forbidden = ${JSON.stringify(forbiddenChildNames)}; process.exit(forbidden.some((name) => process.env[name] !== undefined) ? 1 : 0);`;
for (const childEnv of [apiChildEnv(parentOnly), nextChildEnv(parentOnly)]) {
  const probe = spawnSync(process.execPath, ["-e", probeSource], {
    env: childEnv,
    encoding: "utf8"
  });
  assert.equal(probe.status, 0);
  assert.equal(probe.stdout, "");
  assert.equal(probe.stderr, "");
}
const nextEnv = nextChildEnv(parentOnly);
for (const name of [
  "PLATFORM_DATABASE_URL",
  "PLATFORM_REDIS_URL",
  "PLATFORM_AUTH_E2E_CONTROL_FILE",
  "PLATFORM_TURNSTILE_SECRET_KEY"
]) {
  assert.equal(nextEnv[name], undefined);
}
assert.equal(childEnvSource.includes("const API_NAMES = ["), true);

assert.match(apiLauncher, /apiChildEnv\(\)/u);
assert.match(nextLauncher, /nextChildEnv\(\)/u);
assert.match(apiLauncher, /env,\s*stdio:\s*\["ignore",\s*"ignore",\s*"pipe"\]/u);
assert.match(apiLauncher, /createPythonDiagnosticParser\(platformRoot\)/u);
assert.match(diagnosticsSource, /SAFE_STAGES\.has\(progress\[1\]\)/u);
assert.match(diagnosticsSource, /SAFE_CATEGORIES\.has\(startup\[2\]\)/u);
assert.match(server, /AUTH_E2E_API_STARTUP status=failed stage=\{STARTUP_STAGE\}/u);
assert.match(server, /f"category=\{category\}"/u);
assert.match(server, /endpoint\.__annotations__\["request"\]\s*=\s*Request/u);
assert.match(server, /endpoint\.__annotations__\["return"\]\s*=\s*JSONResponse/u);
assert.match(server, /_startup_stage\("route-registration"\)/u);
const apiServeSource = server.slice(server.indexOf("async def serve()"), server.indexOf("def main()"));
assert.equal((apiServeSource.match(/validate_environment\(\)/gu) ?? []).length, 1);
assert.ok(apiServeSource.indexOf("validate_environment()") < apiServeSource.indexOf("LocalSmtpCapture()"));
assert.match(apiServeSource, /os\.environ\["PLATFORM_EMAIL_SENDER_EMAIL"\]\s*=\s*"Auth E2E/u);
assert.match(apiServeSource, /_require\(settings\.platform_support_smtp_host == "127\.0\.0\.1"/u);
assert.match(server, /if str\(PLATFORM_ROOT\) not in sys\.path:/u);
assert.match(server, /AUTH_E2E_API_PROGRESS stage=\{stage\}/u);
assert.match(apiLauncher, /createPythonDiagnosticParser\(platformRoot\)/u);
assert.match(apiLauncher, /shouldReportEarlyExit\(\{ stopping, spawnFailed \}\)/u);
assert.match(apiLauncher, /shouldInitiateChildStop\(/u);
assert.match(apiLauncher, /child\.on\("exit"/u);
assert.match(apiLauncher, /child\.on\("close"/u);
assert.match(diagnosticsSource, /AUTH_E2E_API_EXIT status=early stage=/u);
assert.doesNotMatch(diagnosticsSource, /process\.stderr\.write\(chunk|console\.(?:log|error)\(line/u);
assert.match(apiLauncher, /apiChildEnv\(\)/u);
assert.doesNotMatch(apiLauncher, /env\s*:\s*\{\s*\.\.\.process\.env/u);
assert.match(nextLauncher, /nextChildEnv\(\)/u);
assert.match(nextLauncher, /env,\s*stdio:\s*"ignore"/u);
assert.doesNotMatch(nextLauncher, /\.\.\.process\.env/u);

assert.match(config, /testMatch:\s*"\*\*\/auth-lifecycle-e2e\.spec\.ts"/u);
assert.match(config, /retries:\s*0/u);
assert.match(config, /trace:\s*"off"/u);
assert.match(config, /video:\s*"off"/u);
assert.match(config, /screenshot:\s*"off"/u);
assert.match(config, /auth-e2e-safe-reporter\.cjs/u);
assert.match(config, /auth-e2e-api-launcher\.cjs/u);
assert.match(config, /auth-e2e-next-launcher\.cjs/u);
assert.match(config, /name: "auth-e2e-api"/u);
assert.match(config, /name: "auth-e2e-next"/u);
assert.match(genericConfig, /sharedIgnoredSpecs\s*=\s*\[[\s\S]*?"auth-lifecycle-e2e\.spec\.ts"/u);
assert.match(genericConfig, /testIgnore:\s*sharedIgnoredSpecs/u);
assert.match(liveConfig, /testIgnore:\s*\["auth-lifecycle-e2e\.spec\.ts"\]/u);
assert.match(reporter, /const EXPECTED_TESTS = new Set\(\[/u);
assert.match(reporter, /function isExpectedTest\(test\)/u);
assert.match(reporter, /onError\(error\)/u);
assert.match(reporter, /process\.argv\.includes\("--list"\)/u);
assert.match(reporter, /this\.discoveryRequested === this\.listMode/u);
assert.match(reporter, /AUTH_E2E_SETUP_ERROR category=\$\{category\} component=\$\{component\} source=\$\{source\} line=\$\{line\}/u);
assert.match(reporter, /AUTH_E2E_API_LAUNCH status=/u);
assert.match(reporter, /AUTH_E2E_NEXT_LAUNCH status=/u);
assert.match(reporter, /AUTH_E2E_DISCOVERY status=\$\{status\} tests=/u);
assert.match(reporter, /return \{ status: "failed" \}/u);
assert.match(reporter, /AUTH_E2E status=\$\{status\} tests=/u);
assert.match(reporter, /AUTH_E2E_FAILURE test=auth-lifecycle stage=\$\{stage\} category=\$\{category\} source=\$\{source\}/u);
assert.match(reporter, /const EXPECTED_STEPS = new Set\(\[/u);
assert.match(reporter, /function failedStage\(steps\)/u);
assert.doesNotMatch(reporter, /process\.stdout\.write\([^\n]*error\.message/u);
assert.match(reporter, /function safeFailureLine\(result\)/u);
assert.doesNotMatch(reporter, /error\.stack|attachments/u);
assert.doesNotMatch(reporter, /process\.stdout\.write\([^\n]*error\.message/u);

const authTitle = "auth lifecycle uses the real API, local mail capture, and strict verification";
const authSpecPath = path.join(webRoot, "tests/smoke/auth-lifecycle-e2e.spec.ts");
const expectedCase = (title = authTitle) => ({
  titlePath: () => ["", "chromium", "tests/smoke/auth-lifecycle-e2e.spec.ts", title],
  location: { file: authSpecPath, line: 177 }
});
function exerciseReporter({
  discoveryFlag,
  listMode,
  cases = [expectedCase()],
  attempts = [],
  serverOutput,
  globalError = false,
  globalErrorMessage = "Timed out waiting for config.webServer."
}) {
  const originalArgv = process.argv;
  const originalCwd = process.cwd();
  const originalFlag = process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY;
  const originalWrite = process.stdout.write;
  let output = "";
  try {
    process.chdir(webRoot);
    if (discoveryFlag === undefined) delete process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY;
    else process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY = discoveryFlag;
    process.argv = listMode
      ? [...originalArgv.filter((arg) => arg !== "--list"), "--list"]
      : originalArgv.filter((arg) => arg !== "--list");
    const reporterInstance = new AuthE2ESafeReporter();
    process.stdout.write = (chunk) => {
      output += String(chunk);
      return true;
    };
    try {
      reporterInstance.onBegin({}, { allTests: () => cases });
    } catch {
      // Playwright's reporter multiplexer catches reporter callback exceptions.
    }
    for (const attempt of attempts) {
      reporterInstance.onTestBegin(attempt.test);
      reporterInstance.onTestEnd(attempt.test, {
        status: attempt.status,
        retry: attempt.retry,
        steps: attempt.steps ?? [],
        errors: attempt.errors ?? []
      });
    }
    if (serverOutput) {
      const chunks = Array.isArray(serverOutput) ? serverOutput : [serverOutput];
      for (const chunk of chunks) reporterInstance.onStdErr(chunk);
    }
    if (globalError) {
      reporterInstance.onError({
        message: globalErrorMessage,
        location: { file: "/outside/source.ts", line: 9 }
      });
    }
    const end = reporterInstance.onEnd({ status: "passed" });
    return { end, output };
  } finally {
    process.stdout.write = originalWrite;
    process.argv = originalArgv;
    process.chdir(originalCwd);
    if (originalFlag === undefined) delete process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY;
    else process.env.PLATFORM_AUTH_E2E_DISCOVERY_ONLY = originalFlag;
  }
}

const expectedAttempt = { test: expectedCase(), status: "passed", retry: 0 };
const normalReporterPass = exerciseReporter({ attempts: [expectedAttempt] });
assert.equal(normalReporterPass.end, undefined);
assert.match(normalReporterPass.output, /AUTH_E2E status=passed tests=1 passed=1 failed=0/u);
const listReporterPass = exerciseReporter({ discoveryFlag: "1", listMode: true });
assert.equal(listReporterPass.end, undefined);
assert.match(listReporterPass.output, /AUTH_E2E_DISCOVERY status=passed tests=1 exact=1/u);
assert.doesNotMatch(listReporterPass.output, /AUTH_E2E status=passed/u);

for (const mismatch of [
  { discoveryFlag: undefined, listMode: true },
  { discoveryFlag: "1", listMode: false }
]) {
  const outcome = exerciseReporter({ ...mismatch, attempts: [expectedAttempt] });
  assert.deepEqual(outcome.end, { status: "failed" });
  assert.match(outcome.output, /AUTH_E2E status=failed/u);
  assert.doesNotMatch(outcome.output, /AUTH_E2E_DISCOVERY status=passed/u);
}

const wrongIdentity = exerciseReporter({ cases: [expectedCase("unexpected title")] });
assert.deepEqual(wrongIdentity.end, { status: "failed" });
assert.match(wrongIdentity.output, /status=invalid-suite tests=1 exact=0/u);
assert.match(wrongIdentity.output, /AUTH_E2E status=failed/u);
const emptySuite = exerciseReporter({ cases: [] });
assert.deepEqual(emptySuite.end, { status: "failed" });
assert.match(emptySuite.output, /status=invalid-suite tests=0 exact=0/u);
const skippedAttempt = exerciseReporter({
  attempts: [{ test: expectedCase(), status: "skipped", retry: 0 }]
});
assert.deepEqual(skippedAttempt.end, { status: "failed" });
assert.match(skippedAttempt.output, /AUTH_E2E status=failed tests=1 .*failed=1/u);
const secretAssertionFailure = exerciseReporter({
  attempts: [{
    test: expectedCase(),
    status: "failed",
    retry: 0,
    steps: [{ title: "01-registration-verification-code", error: { name: "Error" } }],
    errors: [{
      message: "password=secret-value token=secret-token",
      location: { file: authSpecPath, line: 198, column: 7 }
    }]
  }]
});
assert.deepEqual(secretAssertionFailure.end, { status: "failed" });
assert.match(
  secretAssertionFailure.output,
  /AUTH_E2E_FAILURE test=auth-lifecycle stage=01-registration-verification-code category=assertion source=tests\/smoke\/auth-lifecycle-e2e\.spec\.ts#01-registration-verification-code line=198/u
);
assert.doesNotMatch(secretAssertionFailure.output, /secret-value|secret-token|password=|token=/u);
const retriedAttempt = exerciseReporter({
  attempts: [{ test: expectedCase(), status: "passed", retry: 1 }]
});
assert.deepEqual(retriedAttempt.end, { status: "failed" });
assert.match(retriedAttempt.output, /AUTH_E2E status=failed/u);
const setupError = exerciseReporter({ attempts: [expectedAttempt], globalError: true });
assert.deepEqual(setupError.end, { status: "failed" });
assert.match(setupError.output, /AUTH_E2E_SETUP_ERROR category=webserver-setup component=unknown source=unknown line=0/u);
assert.match(setupError.output, /AUTH_E2E status=failed/u);
const namedServerError = exerciseReporter({
  attempts: [expectedAttempt],
  serverOutput: "\u001b[2m[auth-e2e-next] \u001b[0mlauncher could not start\n",
  globalError: true
});
assert.deepEqual(namedServerError.end, { status: "failed" });
assert.match(namedServerError.output, /AUTH_E2E_SETUP_ERROR category=webserver-setup component=next source=unknown line=0/u);
const apiStartupError = exerciseReporter({
  serverOutput: [
    "\u001b[2m[auth-e2e-api] \u001b[0mAUTH_E2E_API_PROGRESS stage=application-",
    "import\n[auth-e2e-api] AUTH_E2E_API_STARTUP status=failed stage=route-registration category=name-error\n"
  ],
  globalError: true
});
assert.deepEqual(apiStartupError.end, { status: "failed" });
assert.match(apiStartupError.output, /AUTH_E2E_API_PROGRESS stage=application-import/u);
assert.match(apiStartupError.output, /AUTH_E2E_API_STARTUP stage=route-registration category=name-error/u);
assert.match(apiStartupError.output, /AUTH_E2E_SETUP_ERROR category=webserver-setup component=api source=unknown line=0/u);
assert.doesNotMatch(apiStartupError.output, /\u001b|auth-e2e-api/u);

const apiExitReporter = exerciseReporter({
  serverOutput: "[auth-e2e-api] AUTH_E2E_API_PROGRESS stage=route-registration\n"
    + "[auth-e2e-api] AUTH_E2E_API_EXIT status=early stage=route-registration "
    + "exception=NameError source=apps/platform_api/app/main.py line=432 code=1 signal=none\n",
  globalError: true,
  globalErrorMessage: "config.webServer did not become ready. secret=do-not-project"
});
assert.deepEqual(apiExitReporter.end, { status: "failed" });
assert.match(apiExitReporter.output, /AUTH_E2E_API_PROGRESS stage=route-registration/u);
assert.match(apiExitReporter.output, /AUTH_E2E_API_EXIT status=early stage=route-registration exception=NameError source=apps\/platform_api\/app\/main\.py line=432 code=1 signal=none/u);
assert.doesNotMatch(apiExitReporter.output, /do-not-project|secret=/u);

const webServerBranches = [
  ["http://127.0.0.1:3199 is already used, config.webServer", "api", "already-in-use", "none"],
  ["Process from config.webServer was not able to start. Exit code: 17 secret-value=do-not-project", "unknown", "spawn-exit", "17"],
  ["Process from config.webServer exited early.", "unknown", "early-exit", "none"],
  ["Timed out waiting 45000ms from config.webServer.", "unknown", "timeout", "none"],
  ["config.webServer failed token=do-not-project", "unknown", "unknown", "none"]
];
for (const [message, component, branch, exit] of webServerBranches) {
  const outcome = exerciseReporter({ globalError: true, globalErrorMessage: message });
  assert.deepEqual(outcome.end, { status: "failed" });
  assert.match(
    outcome.output,
    new RegExp("AUTH_E2E_WEB_SERVER component=" + component + " branch=" + branch + " exit=" + exit, "u")
  );
  assert.doesNotMatch(outcome.output, /secret-value|do-not-project/u);
}

const pythonDiagnostics = createPythonDiagnosticParser(platformRoot);
const secretTraceback = [
  "AUTH_E2E_API_PROGRESS stage=application-import",
  "Traceback (most recent call last):",
  "  File \"" + path.join(platformRoot, "apps/platform_api/app/main.py") + "\", line 432, in create_app",
  "    raise NameError('password=secret-traceback-value')",
  "NameError: password=secret-exception-value token=secret-token-value",
  ""
].join("\n");
const projectedTraceback = pythonDiagnostics.consume(secretTraceback)
  + pythonDiagnostics.exit({ code: 1, signal: null });
assert.match(projectedTraceback, /AUTH_E2E_API_PROGRESS stage=application-import/u);
assert.match(projectedTraceback, /AUTH_E2E_API_EXIT status=early stage=application-import exception=NameError source=apps\/platform_api\/app\/main\.py line=432 code=1 signal=none/u);
assert.doesNotMatch(projectedTraceback, /secret-traceback-value|secret-exception-value|secret-token-value|password=|token=/u);

const externalDiagnostics = createPythonDiagnosticParser(platformRoot);
externalDiagnostics.consume([
  "Traceback (most recent call last):",
  "  File \"/private/runtime/path.py\", line 44, in run",
  "SystemExit: 0",
  ""
].join("\n"));
const earlyZeroExit = externalDiagnostics.exit({ code: 0, signal: null });
assert.match(earlyZeroExit, /status=early .*exception=SystemExit source=dependency line=44 code=0/u);
assert.equal(shouldReportEarlyExit({ stopping: false, spawnFailed: false }), true);
assert.equal(shouldReportEarlyExit({ stopping: true, spawnFailed: false }), false);
assert.equal(shouldReportEarlyExit({ stopping: false, spawnFailed: true }), false);
assert.equal(shouldReportEarlyExit({ stopping: false, spawnFailed: false }), true);
assert.equal(shouldInitiateChildStop({
  stopping: false,
  childExited: false,
  exitCode: null,
  signalCode: null
}), true);
assert.equal(shouldInitiateChildStop({
  stopping: false,
  childExited: true,
  exitCode: 0,
  signalCode: null
}), false);
assert.equal(shouldInitiateChildStop({
  stopping: true,
  childExited: false,
  exitCode: null,
  signalCode: null
}), false);

assert.match(server, /os\.environ\.get\("GITHUB_ACTIONS"\) == "true"/u);
assert.match(server, /database_url\.path\.lstrip\("\/"\) == "platformdb_test"/u);
assert.match(server, /settings\.platform_turnstile_mode == "always"/u);
assert.match(server, /httpx\.MockTransport\(siteverify_fixture\)/u);
assert.match(server, /await verify_turnstile_token\(/u);
assert.match(server, /asyncio\.to_thread\(_send_email_message_sync/u);
assert.match(server, /session_residue/u);
assert.match(server, /audit_residue/u);
assert.doesNotMatch(server, /platform_turnstile_mode\s*=\s*"off"/u);
assert.match(spec, /auth\.email_verification\.confirm/u);
assert.match(spec, /auth\.password_reset\.confirm/u);
assert.match(spec, /wrong-hostname/u);
assert.match(spec, /wrong-action/u);
assert.match(spec, /missing-action/u);
assert.match(spec, /profile-password-change-success/u);
assert.match(spec, /cleanupComplete/u);
const discoveryPosition = hermetic.indexOf("run test:auth-lifecycle-e2e -- --list");
const buildPosition = hermetic.indexOf("run build");
const journeyPosition = hermetic.lastIndexOf("run test:auth-lifecycle-e2e");
const smokePosition = hermetic.indexOf("run test:smoke");
assert.ok(discoveryPosition >= 0 && discoveryPosition < buildPosition);
assert.ok(journeyPosition > buildPosition && journeyPosition !== discoveryPosition);
assert.ok(journeyPosition < smokePosition);
assert.equal((hermetic.match(/PLATFORM_AUTH_E2E_DISCOVERY_ONLY=1/gu) ?? []).length, 1);
assert.match(hermetic, /PLATFORM_AUTH_E2E_DISCOVERY_ONLY=1[\s\\\n]*"\$TOOLS_DIR\/platform_web_npm\.sh" run test:auth-lifecycle-e2e -- --list/u);
assert.match(hermetic, /run test:auth-lifecycle-e2e/u);

process.stdout.write("AUTH_E2E_CONTRACT status=passed\n");
