import { defineConfig } from "@playwright/test";
import path from "node:path";

const webRoot = process.cwd();
const preparedBuildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;
const controlFile = process.env.PLATFORM_AUTH_E2E_CONTROL_FILE;
const outputDir = process.env.PLATFORM_AUTH_E2E_OUTPUT_DIR;
if (!preparedBuildDir || !path.isAbsolute(preparedBuildDir)) {
  throw new Error("Auth E2E requires the prepared hermetic web build.");
}
if (!controlFile || !path.isAbsolute(controlFile) || !outputDir || !path.isAbsolute(outputDir)) {
  throw new Error("Auth E2E private control paths are unavailable.");
}

const standaloneServer = [
  `rm -rf .next/standalone && cp -a "${preparedBuildDir}/standalone" .next/standalone`,
  "rm -rf .next/standalone/.next/static .next/standalone/public",
  "mkdir -p .next/standalone/.next",
  `cp -a "${preparedBuildDir}/static" .next/standalone/.next/static && cp -a "${preparedBuildDir}/public" .next/standalone/public`,
  "../../tools/platform_node.sh tests/support/auth-e2e-next-launcher.cjs"
].join(" && ");

export default defineConfig({
  testDir: "./tests/smoke",
  testMatch: "auth-lifecycle-e2e.spec.ts",
  outputDir,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [[path.join(webRoot, "tests/support/auth-e2e-safe-reporter.cjs")]],
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "off",
    video: "off",
    screenshot: "off"
  },
  webServer: [
    {
      command: "../../tools/platform_node.sh tests/support/auth-e2e-api-launcher.cjs",
      env: {
        PLATFORM_AUTH_E2E: "1",
        PLATFORM_AUTH_E2E_CONTROL_FILE: controlFile,
        PLATFORM_ENV_FILE: "/dev/null",
        PLATFORM_ENVIRONMENT: "test",
        PLATFORM_DATABASE_URL: process.env.PLATFORM_DATABASE_URL,
        PLATFORM_DB_SCHEMA: process.env.PLATFORM_DB_SCHEMA,
        PLATFORM_SECRET_KEY: process.env.PLATFORM_SECRET_KEY,
        PLATFORM_REDIS_URL: process.env.PLATFORM_REDIS_URL,
        PLATFORM_CELERY_BROKER_URL: process.env.PLATFORM_CELERY_BROKER_URL,
        PLATFORM_CELERY_RESULT_BACKEND: process.env.PLATFORM_CELERY_RESULT_BACKEND,
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
        PLATFORM_UPLOAD_DIR: process.env.PLATFORM_UPLOAD_DIR,
        PLATFORM_MEDIA_STAGING_DIR: process.env.PLATFORM_MEDIA_STAGING_DIR
      },
      url: "http://127.0.0.1:3199/api/v1/health/live",
      reuseExistingServer: false,
      timeout: 45_000
    },
    {
      command: standaloneServer,
      env: {
        HOSTNAME: "127.0.0.1",
        PORT: "3100",
        PLATFORM_API_BASE_URL: "http://127.0.0.1:3199/api/v1",
        PLATFORM_ADSENSE_ENABLED: "false"
      },
      url: "http://127.0.0.1:3100",
      reuseExistingServer: false,
      timeout: 90_000
    }
  ]
});
