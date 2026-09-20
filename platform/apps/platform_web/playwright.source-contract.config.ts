import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/smoke",
  testMatch: "frontend-audit-regressions.spec.ts",
  outputDir: "./test-results-source-contract",
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  retries: 0,
  reporter: [["list"]],
  // These assertions read repository source only; they must not boot a web
  // server or accidentally become a browser/runtime smoke contour.
  projects: [{ name: "source-contract" }]
});
