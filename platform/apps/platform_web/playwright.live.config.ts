import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const configuredBaseUrl = process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "http://127.0.0.1";
const productionLiveQa = /^https:\/\/old-sparky\.com\/?$/u.test(configuredBaseUrl);
const secretBearingLiveQa = Boolean(
  process.env.PLATFORM_LIVE_USER_QA_SESSIONS
  || process.env.PLATFORM_QA_BROWSER_GATE_DIR,
);
if (productionLiveQa) {
  const configuredUid = process.env.PLATFORM_LIVE_USER_QA_UID ?? "";
  const expectedUid = /^[1-9][0-9]{0,9}$/u.test(configuredUid)
    ? Number(configuredUid)
    : 0;
  if (
    !Number.isSafeInteger(expectedUid)
    || expectedUid === 0
    || typeof process.geteuid !== "function"
    || process.geteuid() !== expectedUid
  ) {
    throw new Error(
      "Production Playwright must run as the exact dedicated non-root QA user."
    );
  }
}

export default defineConfig({
  testDir: "./tests/smoke",
  outputDir: secretBearingLiveQa && process.env.PLATFORM_QA_BROWSER_GATE_DIR
    ? path.join(process.env.PLATFORM_QA_BROWSER_GATE_DIR, "test-results")
    : "./test-results-live",
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  retries: secretBearingLiveQa || productionLiveQa ? 0 : process.env.CI ? 2 : 0,
  reporter: !secretBearingLiveQa && process.env.CI
    ? [["list"], ["html", { outputFolder: "playwright-report-live", open: "never" }]]
    : [["list"]],
  use: {
    baseURL: configuredBaseUrl,
    trace: secretBearingLiveQa ? "off" : "on-first-retry",
    video: secretBearingLiveQa
      ? "off"
      : process.env.CI ? "retain-on-failure" : "off",
    launchOptions: secretBearingLiveQa || productionLiveQa
      ? { chromiumSandbox: true }
      : undefined,
  },
  projects: [
    {
      name: "live-desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } }
    },
    {
      name: "live-mobile",
      use: { ...devices["Pixel 5"] }
    },
    {
      name: "live-webkit-mobile",
      use: { ...devices["iPhone 13"] }
    }
  ]
});
