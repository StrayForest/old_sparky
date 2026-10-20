"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This helper is CommonJS. */

const { chromium, devices, webkit } = require("@playwright/test");

async function checkEngine(engine, contextOptions) {
  let browser;
  let context;
  try {
    browser = await engine.launch({ headless: true, chromiumSandbox: true });
    context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    await page.goto("about:blank");
    if (page.url() !== "about:blank") {
      throw new Error("engine preflight left the fixed blank page");
    }
  } finally {
    try {
      if (context) await context.close();
    } finally {
      if (browser) await browser.close();
    }
  }
}

async function main() {
  await checkEngine(chromium, {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1000 },
  });
  await checkEngine(webkit, devices["iPhone 13"]);
  process.stdout.write("LIVE_QA_ENGINE_PREFLIGHT_READY schema=1\n");
}

main().catch(() => {
  process.exitCode = 1;
});
