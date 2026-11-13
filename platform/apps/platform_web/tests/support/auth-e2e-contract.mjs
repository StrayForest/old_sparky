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

const require = createRequire(import.meta.url);
const { apiChildEnv, nextChildEnv } = require("./auth-e2e-child-env.cjs");
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
assert.match(apiLauncher, /env,\s*stdio:\s*"ignore"/u);
assert.match(apiLauncher, /apiChildEnv\(\)/u);
assert.doesNotMatch(apiLauncher, /env\s*:\s*\{\s*\.\.\.process\.env/u);
assert.match(nextLauncher, /nextChildEnv\(\)/u);
assert.match(nextLauncher, /env,\s*stdio:\s*"ignore"/u);
assert.doesNotMatch(nextLauncher, /\.\.\.process\.env/u);

assert.match(config, /testMatch:\s*"auth-lifecycle-e2e\.spec\.ts"/u);
assert.match(config, /retries:\s*0/u);
assert.match(config, /trace:\s*"off"/u);
assert.match(config, /video:\s*"off"/u);
assert.match(config, /screenshot:\s*"off"/u);
assert.match(config, /auth-e2e-safe-reporter\.cjs/u);
assert.match(config, /auth-e2e-api-launcher\.cjs/u);
assert.match(config, /auth-e2e-next-launcher\.cjs/u);
assert.match(genericConfig, /sharedIgnoredSpecs\s*=\s*\[[\s\S]*?"auth-lifecycle-e2e\.spec\.ts"/u);
assert.match(genericConfig, /testIgnore:\s*sharedIgnoredSpecs/u);
assert.match(liveConfig, /testIgnore:\s*\["auth-lifecycle-e2e\.spec\.ts"\]/u);
assert.match(reporter, /const EXPECTED_TESTS = new Set\(\[/u);
assert.match(reporter, /AUTH_E2E status=\$\{status\} tests=/u);
assert.match(reporter, /AUTH_E2E_FAILURE test=auth-lifecycle stage=\$\{stage\} category=\$\{category\} source=\$\{source\}/u);
assert.match(reporter, /const EXPECTED_STEPS = new Set\(\[/u);
assert.match(reporter, /function failedStage\(steps\)/u);
assert.doesNotMatch(reporter, /error\.message|result\.errors|attachments/u);

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
assert.match(hermetic, /run test:auth-lifecycle-e2e/u);

process.stdout.write("AUTH_E2E_CONTRACT status=passed\n");
