import { defineConfig, devices } from "@playwright/test";

const preparedBuildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;
const desktopOnlySpecs = [
  "account-email-flow.spec.ts",
  "admin-progressive-tournaments.spec.ts",
  "bracket-manual-refresh.spec.ts",
  "info-server-boundary.spec.ts",
  "password-change-flow.spec.ts",
  "password-manager-auth-form.spec.ts",
  "password-reset-autofill.spec.ts",
  "ready-check-timer.spec.ts",
  "tournament-list-concurrency.spec.ts"
];
const sharedIgnoredSpecs = [
  "frontend-audit-regressions.spec.ts",
  "live-launch.spec.ts",
  "live-user-journey.spec.ts",
  "tournament-participant-progressive.spec.ts"
];
const standaloneWebServerCommand = [
  preparedBuildDir
    ? `rm -rf .next/standalone && cp -a "${preparedBuildDir}/standalone" .next/standalone`
    : "../../tools/platform_web_npm.sh run build",
  "rm -rf .next/standalone/.next/static .next/standalone/public",
  "mkdir -p .next/standalone/.next",
  preparedBuildDir
    ? `cp -a "${preparedBuildDir}/static" .next/standalone/.next/static && cp -a "${preparedBuildDir}/public" .next/standalone/public`
    : "cp -R .next/static .next/standalone/.next/static && cp -R public .next/standalone/public",
  "../../tools/platform_node.sh .next/standalone/server.js"
].join(" && ");

export default defineConfig({
  testDir: "./tests/smoke",
  testIgnore: sharedIgnoredSpecs,
  // Live QA, source contracts and participant-progressive tests own separate
  // contours. Desktop-only regression files are kept out of the responsive
  // projects; platform-routes and profile UI retain explicit viewport coverage.
  outputDir: "./test-results",
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: true,
  // Deterministic CI must expose failures instead of retrying them silently.
  retries: 0,
  reporter: process.env.CI
    ? [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]]
    : [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3100",
    trace: "retain-on-failure",
    video: process.env.CI ? "retain-on-failure" : "off"
  },
  webServer: [
    {
      command: "../../tools/platform_node.sh tests/support/mock-platform-api.mjs",
      env: {
        MOCK_PLATFORM_API_PORT: "3198"
      },
      url: "http://127.0.0.1:3198/api/v1/health/live",
      reuseExistingServer: false,
      timeout: 30_000
    },
    {
      command: "../../tools/platform_node.sh tests/support/mock-profile-proxy.mjs",
      url: "http://127.0.0.1:3199/api/v1/health/live",
      reuseExistingServer: false,
      timeout: 30_000
    },
    {
      command: standaloneWebServerCommand,
      env: {
        HOSTNAME: "127.0.0.1",
        PORT: "3100",
        PLATFORM_API_BASE_URL: "http://127.0.0.1:3199/api/v1",
        PLATFORM_ADSENSE_ENABLED: "false"
      },
      url: "http://127.0.0.1:3100",
      reuseExistingServer: false,
      timeout: 120_000
    }
  ],
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } }
    },
    {
      name: "wide-1300",
      testIgnore: [...sharedIgnoredSpecs, ...desktopOnlySpecs],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1300, height: 900 } }
    },
    {
      name: "tablet-820",
      testIgnore: [...sharedIgnoredSpecs, ...desktopOnlySpecs],
      use: { ...devices["Desktop Chrome"], viewport: { width: 820, height: 1100 } }
    },
    {
      // This is a Chromium device emulation profile, not a real Android
      // Autofill / Google Password Manager environment.
      name: "mobile-layout",
      testIgnore: [...sharedIgnoredSpecs, ...desktopOnlySpecs],
      use: { ...devices["Pixel 5"] }
    }
  ]
});
