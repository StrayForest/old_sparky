import { expect, test } from "@playwright/test";
import {
  createLiveUserBrowserGateCapture,
  installLiveUserBrowserGate,
  liveUserBrowserGateAttachment,
} from "../support/live-user-browser-gate";

const origin = "http://image-inventory.test";
const privateCanary = "private-canary-token-7f94";

test("the shared UserQA observer records closed local browser evidence", async ({ browser }) => {
  const context = await browser.newContext();
  const capture = createLiveUserBrowserGateCapture();
  await installLiveUserBrowserGate(context, "local-fixture", origin, capture, new Set());
  await context.route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") {
      await route.fulfill({
        status: 200,
        contentType: "text/html",
        headers: {
          "content-security-policy": "default-src 'self'; script-src 'self' 'nonce-test'; img-src 'none'; connect-src 'self'",
        },
        body: [
          `<img src="/private-image?token=${privateCanary}">`,
          `<script nonce="test">void fetch('/auth/login?token=${privateCanary}'); void fetch('/private-request?token=${privateCanary}').catch(() => {});</script>`,
          '<script src="/throw.js"></script>',
          '<script>window.inlineCanary = "private inline text";</script>',
        ].join(""),
      });
      return;
    }
    if (url.pathname === "/auth/login") {
      await route.fulfill({ status: 403, body: "private auth response" });
      return;
    }
    if (url.pathname === "/throw.js") {
      await route.fulfill({
        status: 200,
        contentType: "application/javascript",
        body: `throw new Error("${privateCanary}");`,
      });
      return;
    }
    await route.abort("failed");
  });

  try {
    const page = await context.newPage();
    await page.goto(`${origin}/`);
    await expect.poll(() => capture.counts.csp_violation).toBeGreaterThan(0);
    await expect.poll(() => capture.counts.csp_console).toBeGreaterThan(0);
    await expect.poll(() => capture.counts.forbidden_request).toBeGreaterThan(0);
    await expect.poll(() => capture.counts.page_error).toBeGreaterThan(0);
    await expect.poll(() => capture.counts.request_failed).toBeGreaterThan(0);

    capture.phase = "logout";
    await page.evaluate(async (token) => {
      await fetch(`/private-request?token=${token}`).catch(() => undefined);
    }, privateCanary);
    await page.evaluate(async (token) => {
      await Promise.all(Array.from({ length: 260 }, (_, index) => (
        fetch(`/private-request?token=${token}-${index}`).catch(() => undefined)
      )));
    }, privateCanary);
    await expect.poll(() => capture.events_truncated).toBe(true);
    await expect.poll(() => capture.counts_truncated).toBe(true);
    expect(capture.counts.request_failed).toBe(255);

    const attachment = liveUserBrowserGateAttachment(capture);
    expect(attachment.schema).toBe(2);
    expect(attachment.coverage).toBe("journey_logout_before_context_close");
    expect(attachment.counts).toEqual({
      csp_violation: expect.any(Number),
      csp_console: expect.any(Number),
      forbidden_request: expect.any(Number),
      page_error: expect.any(Number),
      request_failed: expect.any(Number),
    });
    expect(attachment.events).toHaveLength(32);
    expect(attachment.events.some((event) => event.phase === "journey")).toBe(true);
    expect(attachment.events.some((event) => event.phase === "logout")).toBe(true);
    const serialized = JSON.stringify(attachment);
    expect(serialized).not.toContain(privateCanary);
    expect(serialized).not.toContain("private inline text");
    expect(serialized).not.toContain("private auth response");
    expect(serialized).not.toContain("/private-request");
    expect(serialized).not.toContain("/auth/login");
  } finally {
    await context.close();
  }
});
