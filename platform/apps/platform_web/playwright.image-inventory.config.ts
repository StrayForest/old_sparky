import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const hermeticBuildDir = process.env.PLATFORM_WEB_HERMETIC_BUILD_DIR;

export default defineConfig({
  testDir: "./tests/image-inventory",
  testMatch: ["live-image-inventory.spec.ts", "live-user-browser-gate.spec.ts"],
  outputDir: hermeticBuildDir
    ? path.join(hermeticBuildDir, "image-inventory-results")
    : "./test-results-image-inventory",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://image-inventory.test",
    trace: "off",
    video: "off",
  },
  projects: [
    {
      name: "webkit-mobile",
      use: { ...devices["iPhone 13"] },
    },
  ],
});
