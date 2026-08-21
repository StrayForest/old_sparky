import { defineConfig, devices } from "@playwright/test";

const standaloneWebServerCommand = [
  "../../tools/platform_web_npm.sh run build",
  "rm -rf .next/standalone/.next/static .next/standalone/public",
  "mkdir -p .next/standalone/.next",
  "cp -R .next/static .next/standalone/.next/static",
  "cp -R public .next/standalone/public",
  "../../tools/platform_node.sh .next/standalone/server.js"
].join(" && ");

export default defineConfig({
  testDir: "./tests/smoke",
  testIgnore: [
    "live-bracket-realtime.spec.ts",
    "live-launch.spec.ts",
    "tournament-participant-progressive.spec.ts"
  ],
  // Legacy broad flows are covered by focused profile/account and tournament
  // smoke tests with explicit authentication state.
  grepInvert: /captain profile uses one full-width dream-slot hero picker|profile editor saves tournament profile through API|registered player outside the published roster sees the unassigned state/u,
  outputDir: "./test-results",
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI
    ? [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]]
    : [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3100",
    trace: "on-first-retry",
    video: process.env.CI ? "retain-on-failure" : "off"
  },
  webServer: [
    {
      command: "../../tools/platform_node.sh tests/support/mock-platform-api.mjs",
      env: {
        MOCK_PLATFORM_API_PORT: "3198"
      },
      url: "http://127.0.0.1:3198/api/v1/health/live",
      reuseExistingServer: true,
      timeout: 30_000
    },
    {
      command: "../../tools/platform_node.sh tests/support/mock-profile-proxy.mjs",
      url: "http://127.0.0.1:3199/api/v1/health/live",
      reuseExistingServer: true,
      timeout: 30_000
    },
    {
      command: standaloneWebServerCommand,
      env: {
        HOSTNAME: "127.0.0.1",
        PORT: "3100",
        PLATFORM_API_BASE_URL: "http://127.0.0.1:3199/api/v1"
      },
      url: "http://127.0.0.1:3100",
      reuseExistingServer: true,
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
      use: { ...devices["Desktop Chrome"], viewport: { width: 1300, height: 900 } }
    },
    {
      name: "tablet-820",
      use: { ...devices["Desktop Chrome"], viewport: { width: 820, height: 1100 } }
    },
    {
      // This is a Chromium device emulation profile, not a real Android
      // Autofill / Google Password Manager environment.
      name: "mobile-layout",
      use: { ...devices["Pixel 5"] }
    }
  ]
});
