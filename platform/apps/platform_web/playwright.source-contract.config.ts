import { defineConfig } from "@playwright/test";

const hermeticTimingReporter = "./tests/support/hermetic-timing-reporter.mjs";

export default defineConfig({
  testDir: "./tests/smoke",
  testMatch: [
    "frontend-audit-regressions.spec.ts",
    "origin-validator-contract.spec.ts"
  ],
  outputDir: "./test-results-source-contract",
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  retries: 0,
  reporter: [["list"], [hermeticTimingReporter]],
  // These assertions read repository source only; they must not boot a web
  // server or accidentally become a browser/runtime smoke contour.
  projects: [{ name: "source-contract" }]
});
