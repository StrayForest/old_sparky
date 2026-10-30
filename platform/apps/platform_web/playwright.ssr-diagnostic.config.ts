import { execFileSync } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { defineConfig, devices } from "@playwright/test";

const webRoot = __dirname;
const repositoryRoot = resolve(webRoot, "../../..");
const preparedBuildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;
const fixtureRoot = join(
  tmpdir(),
  `platform-ssr-diagnostic-e2e-${process.pid}-${randomUUID()}`,
);
const runId = randomBytes(16).toString("hex");
const requestId = randomBytes(16).toString("hex");
const invocationId = randomUUID();
const sourceSha = execFileSync("git", ["rev-parse", "HEAD"], {
  cwd: repositoryRoot,
  encoding: "ascii",
}).trim();
const fixturePreload = resolve(
  webRoot,
  "tests/support/ssr-diagnostic-plan-fixture.cjs",
);
const runtimeGuard = resolve(webRoot, "server-shutdown-guard.cjs");

// Playwright workers need the same private fixture identity as the standalone
// Next server, but no fixture file is written into the production plan path.
process.env.PLATFORM_SSR_DIAGNOSTIC_FIXTURE_ROOT = fixtureRoot;
process.env.PLATFORM_SSR_DIAGNOSTIC_RUN_ID = runId;
process.env.PLATFORM_SSR_DIAGNOSTIC_REQUEST_ID = requestId;

const standaloneServerCommand = [
  preparedBuildDir
    ? `rm -rf .next/standalone && cp -a "${preparedBuildDir}/standalone" .next/standalone`
    : "../../tools/platform_web_npm.sh run build",
  "rm -rf .next/standalone/.next/static .next/standalone/public",
  "mkdir -p .next/standalone/.next",
  preparedBuildDir
    ? `cp -a "${preparedBuildDir}/static" .next/standalone/.next/static && cp -a "${preparedBuildDir}/public" .next/standalone/public`
    : "cp -R .next/static .next/standalone/.next/static && cp -R public .next/standalone/public",
  `../../tools/platform_node.sh --require "${runtimeGuard}" --require "${fixturePreload}" .next/standalone/server.js`,
].join(" && ");

export default defineConfig({
  testDir: "./tests/ssr-diagnostic-e2e",
  testMatch: "**/*.spec.ts",
  outputDir: "./test-results-ssr-diagnostic",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "../../tools/platform_node.sh tests/support/mock-platform-api.mjs",
      env: {
        MOCK_PLATFORM_API_PORT: "3198",
        PLATFORM_WEB_HERMETIC_RUN_ID: process.env.PLATFORM_WEB_HERMETIC_RUN_ID ?? "",
      },
      url: "http://127.0.0.1:3198/api/v1/health/live",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "../../tools/platform_node.sh tests/support/mock-profile-proxy.mjs",
      env: {
        PLATFORM_WEB_HERMETIC_RUN_ID: process.env.PLATFORM_WEB_HERMETIC_RUN_ID ?? "",
      },
      url: "http://127.0.0.1:3199/api/v1/health/live",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: standaloneServerCommand,
      env: {
        HOSTNAME: "127.0.0.1",
        PORT: "3100",
        PLATFORM_API_BASE_URL: "http://127.0.0.1:3199/api/v1",
        PLATFORM_ADSENSE_ENABLED: "false",
        PLATFORM_WEB_HERMETIC_RUN_ID: process.env.PLATFORM_WEB_HERMETIC_RUN_ID ?? "",
        PLATFORM_RUNTIME_SERVICE: "web",
        INVOCATION_ID: invocationId,
        PLATFORM_SSR_DIAGNOSTIC_E2E_FIXTURE: "1",
        PLATFORM_SSR_DIAGNOSTIC_FIXTURE_ROOT: fixtureRoot,
        PLATFORM_SSR_DIAGNOSTIC_RUN_ID: runId,
        PLATFORM_SSR_DIAGNOSTIC_REQUEST_ID: requestId,
        PLATFORM_SSR_DIAGNOSTIC_SOURCE_SHA: sourceSha,
      },
      url: "http://127.0.0.1:3100",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
  projects: [{ name: "ssr-diagnostic", use: { ...devices["Desktop Chrome"] } }],
});
