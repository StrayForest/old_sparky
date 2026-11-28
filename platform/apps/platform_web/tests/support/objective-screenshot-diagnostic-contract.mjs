import assert from "node:assert/strict";

async function main() {
  const {
    OBJECTIVE_SCREENSHOT_ATTACHMENT_NAME,
    classifyObjectiveScreenshotError,
    takeObjectiveScreenshotWithDiagnostics,
  } = await import("./objective-screenshot-diagnostic.ts");
  const protocolPrefix = "Protocol error (Page.captureScreenshot): ";
  const cases = [
    [new Error("Test timeout of 30000ms exceeded"), true, "test_timeout"],
    [new Error("page.screenshot: Timeout 30000ms exceeded"), true, "capture_timeout"],
    [Object.assign(new Error("target closed"), { name: "TargetClosedError" }), true, "browser_closed_or_crashed"],
    [new Error("Page.captureScreenshot is not supported"), true, "capture_unavailable"],
    [new Error("Cannot take screenshot while page is navigating"), true, "page_navigating"],
    [new Error("Test ended."), true, "context_closed"],
    [new Error("page.screenshot: Test ended."), true, "context_closed"],
    [new Error("page.screenshot:\tTest ended."), true, "other"],
    [new Error(`${protocolPrefix}Screenshot failed`), true, "protocol_capture_error"],
    [new Error(`page.screenshot: ${protocolPrefix}Screenshot failed`), true, "protocol_capture_error"],
    [new Error(`${protocolPrefix}Unable to capture screenshot`), true, "capture_empty_image"],
    [new Error(`page.screenshot: ${protocolPrefix}Page is too large.`), true, "capture_dimensions_too_large"],
    [new Error(`${protocolPrefix}Cannot take screenshot with 0 width.`), true, "capture_invalid_clip_dimension"],
    [new Error(`page.screenshot: ${protocolPrefix}Cannot take screenshot with 0 height.`), true, "capture_invalid_clip_dimension"],
    [new Error(`${protocolPrefix}Unable to capture screenshot; /private/path`), true, "protocol_capture_error"],
    [new Error(`private-marker ${protocolPrefix}Unable to capture screenshot`), true, "other"],
    [new Error("https://private.invalid/Test ended."), true, "other"],
    [new Error("private-marker Protocol error (Page.captureScreenshot): failed"), true, "other"],
    [new Error("page.screenshot: Test ended. /tmp/private-path"), true, "other"],
    [new Error("Invalid screenshot clip width"), true, "invalid_geometry"],
    [Object.assign(new Error("permission denied"), { code: "EACCES" }), true, "output_eacces"],
    [Object.assign(new Error("read-only file system"), { code: "EROFS" }), true, "output_erofs"],
    [Object.assign(new Error("no space left on device"), { code: "ENOSPC" }), true, "output_enospc"],
    [Object.assign(new Error("file too large"), { code: "EFBIG" }), true, "output_efbig"],
    [Object.assign(new Error("no such file or directory"), { code: "ENOENT" }), true, "output_enoent"],
    [new Error("unclassified private-marker /tmp/private-path"), true, "other"],
  ];
  for (const [error, connected, expected] of cases) {
    assert.equal(classifyObjectiveScreenshotError(error, connected), expected);
  }
  assert.equal(
    classifyObjectiveScreenshotError(new Error("Test ended."), true, "resolve_options"),
    "other",
  );
  assert.equal(
    classifyObjectiveScreenshotError(
      new Error(`${protocolPrefix}Unable to capture screenshot`),
      true,
      "resolve_options",
    ),
    "other",
  );
  assert.equal(
    classifyObjectiveScreenshotError(
      new Error(`page.screenshot: ${protocolPrefix}Page is too large.`),
      true,
      "resolve_options",
    ),
    "other",
  );
  for (const tail of [
    "Cannot take screenshot with 0 width.",
    "Cannot take screenshot with 0 height.",
  ]) {
    assert.equal(
      classifyObjectiveScreenshotError(
        new Error(`page.screenshot: ${protocolPrefix}${tail}`),
        true,
        "resolve_options",
      ),
      "other",
    );
  }

  const original = new Error("private-marker /tmp/private-path https://private.invalid/x");
  let attachment;
  let evaluateCalls = 0;
  const browser = { isConnected: () => true };
  const page = {
    viewportSize: () => ({ width: 1440, height: 1000 }),
    evaluate: async () => {
      evaluateCalls += 1;
      return 61_234;
    },
    screenshot: async () => { throw original; },
    isClosed: () => false,
    context: () => ({ browser: () => browser }),
  };
  const testInfo = {
    attach: async (name, options) => { attachment = { name, options }; },
  };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(page, testInfo, () => ({ fullPage: true })),
    (error) => error === original,
  );
  assert.equal(evaluateCalls, 1);
  assert.equal(attachment.name, OBJECTIVE_SCREENSHOT_ATTACHMENT_NAME);
  const payload = JSON.parse(attachment.options.body.toString("utf8"));
  assert.deepEqual(Object.keys(payload).sort(), [
    "browser_connected", "category", "crash_observed", "document_height_bucket",
    "elapsed_bucket", "error_code", "error_name", "page_closed", "phase", "schema", "viewport",
  ]);
  assert.equal(payload.schema, 1);
  assert.equal(payload.category, "other");
  assert.equal(payload.error_name, "Error");
  assert.equal(payload.phase, "capture");
  assert.equal(payload.error_code, "none");
  assert.equal(payload.document_height_bucket, ">=60k");
  assert.equal(payload.viewport.width, 1440);
  assert.equal(payload.viewport.height, 1000);
  assert.equal(JSON.stringify(payload).includes("private-marker"), false);
  assert.equal(JSON.stringify(payload).includes("private-path"), false);
  assert.equal(JSON.stringify(payload).includes("private.invalid"), false);

  const exactProtocolCases = [
    ["Unable to capture screenshot", "capture_empty_image"],
    ["Page is too large.", "capture_dimensions_too_large"],
    ["Cannot take screenshot with 0 width.", "capture_invalid_clip_dimension"],
    ["Cannot take screenshot with 0 height.", "capture_invalid_clip_dimension"],
  ];
  for (const [tail, category] of exactProtocolCases) {
    const protocolError = new Error(`page.screenshot: ${protocolPrefix}${tail}`);
    let protocolAttachment;
    await assert.rejects(
      takeObjectiveScreenshotWithDiagnostics({
        ...page,
        screenshot: async () => { throw protocolError; },
      }, {
        attach: async (name, options) => { protocolAttachment = { name, options }; },
      }, () => ({ fullPage: true })),
      (error) => error === protocolError,
    );
    assert.equal(protocolAttachment.name, OBJECTIVE_SCREENSHOT_ATTACHMENT_NAME);
    const protocolPayload = JSON.parse(protocolAttachment.options.body.toString("utf8"));
    assert.equal(protocolPayload.category, category);
    assert.equal(protocolPayload.error_code, "none");
    assert.equal(protocolPayload.phase, "capture");
    assert.equal(JSON.stringify(protocolPayload).includes(tail), false);
  }

  const resolverError = new Error("Test ended.");
  let resolverScreenshotCalled = false;
  let resolverFailureAttachment;
  const resolverFailurePage = {
    ...page,
    screenshot: async () => {
      resolverScreenshotCalled = true;
      throw original;
    },
  };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(resolverFailurePage, {
      attach: async (name, options) => { resolverFailureAttachment = { name, options }; },
    }, () => { throw resolverError; }),
    (error) => error === resolverError,
  );
  assert.equal(resolverScreenshotCalled, false);
  const resolverFailurePayload = JSON.parse(resolverFailureAttachment.options.body.toString("utf8"));
  assert.equal(resolverFailurePayload.phase, "resolve_options");
  assert.equal(resolverFailurePayload.category, "other");

  const viewportError = new Error("viewport query failed");
  let viewportScreenshotCalled = false;
  let viewportFailureAttachment;
  const viewportFailurePage = {
    ...page,
    viewportSize: () => { throw viewportError; },
    screenshot: async () => {
      viewportScreenshotCalled = true;
      throw original;
    },
  };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(viewportFailurePage, {
      attach: async (name, options) => { viewportFailureAttachment = { name, options }; },
    }, () => ({ fullPage: true })),
    (error) => error === original,
  );
  assert.equal(viewportScreenshotCalled, true);
  const viewportFailurePayload = JSON.parse(viewportFailureAttachment.options.body.toString("utf8"));
  assert.deepEqual(viewportFailurePayload.viewport, { width: null, height: null });

  const pathError = Object.assign(new Error("private output path resolver failure"), { code: "ENOENT" });
  let pathScreenshotCalled = false;
  let pathFailureAttachment;
  const pathFailurePage = {
    ...page,
    screenshot: async () => {
      pathScreenshotCalled = true;
      return Buffer.alloc(0);
    },
  };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(pathFailurePage, {
      attach: async (name, options) => { pathFailureAttachment = { name, options }; },
    }, () => { throw pathError; }),
    (error) => error === pathError,
  );
  assert.equal(pathScreenshotCalled, false);
  const pathFailurePayload = JSON.parse(pathFailureAttachment.options.body.toString("utf8"));
  assert.equal(pathFailurePayload.category, "output_enoent");
  assert.equal(pathFailurePayload.phase, "resolve_options");
  assert.equal(pathFailurePayload.error_code, "ENOENT");
  assert.equal(JSON.stringify(pathFailurePayload).includes("output path"), false);

  const unknownCodeError = Object.assign(new Error("private error code"), { code: "PRIVATE_CODE" });
  let unknownCodeAttachment;
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics({
      ...page,
      screenshot: async () => { throw unknownCodeError; },
    }, {
      attach: async (name, options) => { unknownCodeAttachment = { name, options }; },
    }, () => ({ fullPage: true })),
    (error) => error === unknownCodeError,
  );
  const unknownCodePayload = JSON.parse(unknownCodeAttachment.options.body.toString("utf8"));
  assert.equal(unknownCodePayload.error_code, "other");
  assert.equal(JSON.stringify(unknownCodePayload).includes("PRIVATE_CODE"), false);

  const attachFailure = new Error("secondary private attachment failure");
  const closedPage = {
    ...page,
    screenshot: async () => { throw original; },
    isClosed: () => true,
    evaluate: async () => { throw new Error("closed page must not be evaluated after capture"); },
  };
  const failingTestInfo = { attach: async () => { throw attachFailure; } };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(closedPage, failingTestInfo, () => ({ fullPage: true })),
    (error) => error === original,
  );

  let invalidViewportAttachment;
  const invalidViewportPage = {
    ...page,
    viewportSize: () => ({ width: 0, height: 10_001 }),
  };
  await assert.rejects(
    takeObjectiveScreenshotWithDiagnostics(invalidViewportPage, {
      attach: async (name, options) => { invalidViewportAttachment = { name, options }; },
    }, () => ({ fullPage: true })),
    (error) => error === original,
  );
  const invalidViewportPayload = JSON.parse(invalidViewportAttachment.options.body.toString("utf8"));
  assert.deepEqual(invalidViewportPayload.viewport, { width: null, height: null });

  let hangingScreenshotCalled = false;
  let hangingAttachment;
  let deadlineTimer;
  const hangingPage = {
    ...page,
    evaluate: () => new Promise(() => {}),
    screenshot: async () => {
      hangingScreenshotCalled = true;
      throw original;
    },
  };
  const hangingCapture = takeObjectiveScreenshotWithDiagnostics(hangingPage, {
    attach: async (name, options) => { hangingAttachment = { name, options }; },
  }, () => ({ fullPage: true }));
  const outerDeadline = new Promise((_, reject) => {
    deadlineTimer = setTimeout(() => reject(new Error("bounded screenshot probe exceeded outer deadline")), 5_000);
  });
  try {
    await Promise.race([
      assert.rejects(hangingCapture, (error) => error === original),
      outerDeadline,
    ]);
  } finally {
    clearTimeout(deadlineTimer);
  }
  assert.equal(hangingScreenshotCalled, true);
  const hangingPayload = JSON.parse(hangingAttachment.options.body.toString("utf8"));
  assert.equal(hangingPayload.document_height_bucket, "unavailable");
}

main().catch((error) => {
  process.stderr.write(`objective screenshot diagnostic contract failed: ${error.name}\n`);
  process.exitCode = 1;
});
