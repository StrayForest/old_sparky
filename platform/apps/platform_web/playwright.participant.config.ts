import { defineConfig, devices } from "@playwright/test";

const apiPort = 18019;
const webPort = 3101;
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
  testMatch: "tournament-participant-progressive.spec.ts",
  outputDir: "./test-results-participant",
  timeout: 60_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: "on-first-retry"
  },
  webServer: {
    command: standaloneWebServerCommand,
    env: {
      HOSTNAME: "127.0.0.1",
      PORT: String(webPort),
      PLATFORM_API_BASE_URL: `http://127.0.0.1:${apiPort}/api/v1`,
      PLATFORM_ADSENSE_ENABLED: "false"
    },
    url: `http://127.0.0.1:${webPort}/auth/login`,
    reuseExistingServer: false,
    timeout: 120_000
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } }
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"] }
    }
  ]
});
