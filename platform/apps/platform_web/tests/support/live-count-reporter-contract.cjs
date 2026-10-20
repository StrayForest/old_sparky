"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This contract runs as CommonJS. */

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  bindingsFromEnvironment,
  summarizeRun,
  writeBoundedSummary,
  writeBoundedFailureSummary,
  createPageErrorAttachment,
  trustedSuccessfulNextScriptPath,
  pageErrorClass,
} = require("./live-count-reporter.cjs");

function test(outcome, results = [], expectedStatus = "passed") {
  return {
    title: "private title must never be serialized",
    id: "private-playwright-id",
    expectedStatus,
    results: results.map((status) => ({ status, error: { message: "private error" } })),
    outcome: () => outcome,
  };
}

const suite = {
  allTests: () => [
    test("expected", ["passed"]),
    test("flaky", ["failed", "passed"]),
    test("unexpected", ["failed"]),
    test("expected", ["failed"], "failed"),
    test("skipped", [], "skipped"),
    test("unexpected", ["interrupted"]),
    test("skipped", []),
    test("expected", []),
  ],
};
const summary = summarizeRun(suite, "interrupted");
assert.deepEqual(summary, {
  run_status: "interrupted",
  logical: { total: 8, pass: 1, fail: 1, expected_fail: 1, flaky: 1, skip: 2, interrupted: 2 },
  attempts: {
    total: 6,
    pass: 2,
    fail: 3,
    skip: 0,
    interrupted: 1,
    timedout: 0,
  },
});
assert.equal(summarizeRun({ allTests: () => [test("unknown", ["mystery"])] }, "failed"), null);
assert.equal(
  summarizeRun({ allTests: () => [test("expected", [])] }, "failed").logical.interrupted,
  1,
);
assert.equal(summarizeRun({ allTests: () => Array(4097).fill(test("expected", ["passed"])) }, "passed"), null);

function publicCase(title, project, outcome, description) {
  return {
    title,
    expectedStatus: outcome === "skipped" ? "skipped" : "passed",
    parent: { project: () => ({ name: project }) },
    results: [
      {
        status: outcome === "skipped" ? "skipped" : "passed",
        annotations: description === undefined
          ? []
          : [{ type: "public-live-qa-expected-skip", description }],
      },
    ],
    outcome: () => outcome,
  };
}

const publicProjects = ["live-desktop", "live-mobile", "live-webkit-mobile"];
const publicTitles = [
  ...["/", "/info", "/tournaments", "/tournaments/new", "/profile/me", "/profile/lisalexy"]
    .map((route) => `live route ${route} renders without horizontal overflow`),
  "production Chromium live QA keeps its process sandbox enabled",
  "live CSP nonce is stable on soft navigation and rotates on hard reload",
  "local enforced CSP blocks negative inline and external probes",
  "live tournaments hub exposes a valid empty or populated list",
  "live 1920 catalog stays contained after every card asset loads",
  "live tournament detail and bracket routes render from the current public data",
  "live admin route is protected for anonymous users",
  "live home uses text-only tournament steps without overflow",
  "live public contact surfaces do not publish the support recipient",
  "live current patch route and objective source icons remain available",
  "live Cloudflare Analytics is conditionally observed without widening CSP",
  "live image currentSrc inventory stays inside the exact CSP hosts",
];
const expectedPublicSkips = new Map([
  ["production Chromium live QA keeps its process sandbox enabled|live-webkit-mobile", "chromium_only:live-webkit-mobile"],
  ...publicProjects.map((project) => [
    `local enforced CSP blocks negative inline and external probes|${project}`,
    `local_csp_disabled:${project}`,
  ]),
  ...["live-mobile", "live-webkit-mobile"].map((project) => [
    `live 1920 catalog stays contained after every card asset loads|${project}`,
    `desktop_only:${project}`,
  ]),
  ["live 1920 catalog stays contained after every card asset loads|live-desktop", "empty_tournament_list:live-desktop"],
  ...publicProjects.map((project) => [
    `live tournament detail and bracket routes render from the current public data|${project}`,
    `empty_tournament_list:${project}`,
  ]),
]);
const publicTests = publicProjects.flatMap((project) => publicTitles.map((title) => {
  const skipDescription = expectedPublicSkips.get(`${title}|${project}`);
  return publicCase(
    title,
    project,
    skipDescription === undefined ? "expected" : "skipped",
    skipDescription,
  );
}));
const publicSuite = { allTests: () => publicTests };
assert.equal(summarizeRun(publicSuite, "passed", { publicGate: true }).run_status, "passed");
const unannotatedSkip = publicCase(
  "live route / renders without horizontal overflow",
  "live-desktop",
  "skipped",
);
assert.equal(
  summarizeRun(
    { allTests: () => [unannotatedSkip, ...publicTests.slice(1)] },
    "passed",
    { publicGate: true },
  ).run_status,
  "failed",
  "an unannotated skipped selected case invalidates the public run",
);
const wrongProjectSkip = publicCase(
  "production Chromium live QA keeps its process sandbox enabled",
  "live-mobile",
  "skipped",
  "chromium_only:live-webkit-mobile",
);
const wrongProjectTests = [...publicTests];
const sandboxWebkitIndex = wrongProjectTests.findIndex((testCase) => (
  testCase.title === wrongProjectSkip.title
  && testCase.parent.project().name === "live-webkit-mobile"
));
assert.notEqual(sandboxWebkitIndex, -1);
wrongProjectTests[sandboxWebkitIndex] = wrongProjectSkip;
assert.equal(
  summarizeRun(
    { allTests: () => wrongProjectTests },
    "passed",
    { publicGate: true },
  ).run_status,
  "failed",
  "skip annotations are bound to the exact project as well as the test",
);
assert.equal(
  summarizeRun(
    { allTests: () => publicTests.slice(0, -1) },
    "passed",
    { publicGate: true },
  ).run_status,
  "failed",
  "a missing selected case cannot be hidden by expected skips",
);
const duplicatePublicTests = [...publicTests];
duplicatePublicTests[0] = duplicatePublicTests[1];
assert.equal(
  summarizeRun(
    { allTests: () => duplicatePublicTests },
    "passed",
    { publicGate: true },
  ).run_status,
  "failed",
  "a duplicate selected test cannot replace a missing planned case",
);
const liveUserSummary = summarizeRun(
  { allTests: () => [test("expected", ["passed"])] },
  "passed",
  { publicGate: false },
);
assert.deepEqual(liveUserSummary, {
  run_status: "passed",
  logical: { total: 1, pass: 1, fail: 0, expected_fail: 0, flaky: 0, skip: 0, interrupted: 0 },
  attempts: { total: 1, pass: 1, fail: 0, skip: 0, interrupted: 0, timedout: 0 },
});
let cappedResultReads = 0;
const cappedResults = Array.from({ length: 32769 }, () => ({
  get status() {
    cappedResultReads += 1;
    return "passed";
  },
}));
assert.equal(
  summarizeRun({
    allTests: () => [{ expectedStatus: "passed", results: cappedResults, outcome: () => "expected" }],
  }, "passed"),
  null,
);
assert.equal(cappedResultReads, 32768, "attempt cap is enforced before reading another result");

const liveChunkUrl = "https://old-sparky.com/_next/static/chunks/abcdefghijklm.js";
const successfulScriptResponse = {
  url: liveChunkUrl,
  status: 200,
  resourceType: "script",
};
assert.equal(
  trustedSuccessfulNextScriptPath(successfulScriptResponse),
  "/_next/static/chunks/abcdefghijklm.js",
  "the strict chunk grammar accepts a hashed Next chunk basename",
);
for (const response of [
  { ...successfulScriptResponse, status: 404 },
  { ...successfulScriptResponse, resourceType: "image" },
  { ...successfulScriptResponse, url: "https://third-party.invalid/_next/static/chunks/abcdefghijklm.js" },
  { ...successfulScriptResponse, url: `${liveChunkUrl}?token=private` },
  { ...successfulScriptResponse, url: "https://old-sparky.com/_next/static/chunks/not-a-next-chunk.js" },
]) {
  assert.equal(trustedSuccessfulNextScriptPath(response), null);
}
const pageError = new TypeError("private pageerror text and token");
pageError.stack = `TypeError: private pageerror text and token\n    at render (${liveChunkUrl}:12:34)`;
const pageErrorAttachment = createPageErrorAttachment({
  pageUrl: "https://old-sparky.com/patches/12345?marker=private",
  errors: [pageError],
  successfulScriptResponses: [successfulScriptResponse],
});
assert.ok(pageErrorAttachment);
assert.equal(pageErrorAttachment.name, "live-pageerror-evidence-v1");
assert.equal(pageErrorAttachment.contentType, "application/json");
assert.equal(Buffer.isBuffer(pageErrorAttachment.body), true);
assert.deepEqual(JSON.parse(pageErrorAttachment.body.toString("ascii")), {
  schema: 1,
  truncated: false,
  events: [{
    class: "TypeError",
    route: "patch",
    asset_basename: "abcdefghijklm.js",
    line: 12,
    column: 34,
  }],
});
assert.equal(pageErrorClass({ name: "SecurityError" }), "DOMException");
for (const privateValue of [
  "private pageerror text",
  "token=private",
  "12345",
  "https://old-sparky.com",
]) {
  assert.equal(pageErrorAttachment.body.includes(Buffer.from(privateValue)), false);
}
const unmatchedPageError = createPageErrorAttachment({
  pageUrl: "https://old-sparky.com/",
  errors: [pageError],
  successfulScriptResponses: [{ ...successfulScriptResponse, status: 500 }],
});
assert.deepEqual(JSON.parse(unmatchedPageError.body.toString("ascii")).events[0], {
  class: "TypeError",
  route: "home",
  asset_basename: null,
  line: null,
  column: null,
});
const unknownPageError = new Error("private message");
unknownPageError.name = "private dynamic error name";
assert.equal(pageErrorClass(unknownPageError), "unknown");
assert.deepEqual(
  JSON.parse(createPageErrorAttachment({
    pageUrl: "https://old-sparky.com/",
    errors: [
      unknownPageError,
      new Error("another private error"),
      new Error("third private error"),
      new Error("fourth private error"),
      new Error("fifth private error"),
    ],
    successfulScriptResponses: [],
  }).body.toString("ascii")),
  {
    schema: 1,
    truncated: true,
    events: ["unknown", "Error", "Error", "Error"].map((errorClass) => ({
      class: errorClass,
      route: "home",
      asset_basename: null,
      line: null,
      column: null,
    })),
  },
  "the real collector caps events and projects unknown names without dynamic text",
);

const bindingEnv = {
  PLATFORM_QA_BROWSER_GATE_DIR: "/run/oldsparky-liveqa/public-live-qa.a1b2c3d4",
  PLATFORM_LIVE_QA_TARGET_SHA: "a".repeat(40),
  PLATFORM_LIVE_QA_RUNNER_SHA: "b".repeat(40),
  PLATFORM_LIVE_QA_MARKER_SHA256: "c".repeat(64),
};
assert.deepEqual(bindingsFromEnvironment(bindingEnv), {
  gate: bindingEnv.PLATFORM_QA_BROWSER_GATE_DIR,
  app_sha: bindingEnv.PLATFORM_LIVE_QA_TARGET_SHA,
  source_sha: bindingEnv.PLATFORM_LIVE_QA_RUNNER_SHA,
  marker_sha256: bindingEnv.PLATFORM_LIVE_QA_MARKER_SHA256,
});
const userBindingEnv = {
  ...bindingEnv,
  PLATFORM_QA_BROWSER_GATE_DIR: "/run/oldsparky-liveqa/live-user-qa.Abc123",
  PLATFORM_LIVE_USER_QA_SESSIONS: "/run/oldsparky-liveqa/live-user-qa.Abc123/browser-sessions.json",
};
assert.deepEqual(bindingsFromEnvironment(userBindingEnv), {
  gate: userBindingEnv.PLATFORM_QA_BROWSER_GATE_DIR,
  app_sha: userBindingEnv.PLATFORM_LIVE_QA_TARGET_SHA,
  source_sha: userBindingEnv.PLATFORM_LIVE_QA_RUNNER_SHA,
  marker_sha256: userBindingEnv.PLATFORM_LIVE_QA_MARKER_SHA256,
});
assert.equal(
  bindingsFromEnvironment({
    ...bindingEnv,
    PLATFORM_LIVE_USER_QA_SESSIONS: `${bindingEnv.PLATFORM_QA_BROWSER_GATE_DIR}/browser-sessions.json`,
  }),
  null,
  "public mode rejects a live-user sessions binding",
);
assert.equal(
  bindingsFromEnvironment({ ...userBindingEnv, PLATFORM_LIVE_USER_QA_SESSIONS: undefined }),
  null,
  "live-user mode requires its exact sessions path",
);
assert.equal(
  bindingsFromEnvironment({ ...userBindingEnv, PLATFORM_LIVE_USER_QA_SESSIONS: "/tmp/browser-sessions.json" }),
  null,
  "live-user mode rejects a sessions path outside its gate",
);
assert.equal(bindingsFromEnvironment({ ...bindingEnv, PLATFORM_QA_BROWSER_GATE_DIR: "/tmp/public-live-qa.a1b2c3d4" }), null);
assert.equal(bindingsFromEnvironment({ ...userBindingEnv, PLATFORM_QA_BROWSER_GATE_DIR: "/run/oldsparky-liveqa/live-user-qa.Abc12" }), null);
for (const [label, gate] of [
  ["short public gate", "/run/oldsparky-liveqa/public-live-qa.abc"],
  ["uppercase public gate", "/run/oldsparky-liveqa/public-live-qa.Abc2c3d4"],
  ["short live-user gate", "/run/oldsparky-liveqa/live-user-qa.Abc12"],
  ["invalid live-user character", "/run/oldsparky-liveqa/live-user-qa.Abc_23"],
  ["unrecognized gate", "/run/oldsparky-liveqa/private-qa.Abc123"],
]) {
  assert.equal(
    bindingsFromEnvironment({ ...bindingEnv, PLATFORM_QA_BROWSER_GATE_DIR: gate }),
    null,
    `${label} is rejected`,
  );
}
for (const [label, bindingPatch] of [
  ["missing app SHA", { PLATFORM_LIVE_QA_TARGET_SHA: undefined }],
  ["invalid app SHA", { PLATFORM_LIVE_QA_TARGET_SHA: "bad" }],
  ["missing runner SHA", { PLATFORM_LIVE_QA_RUNNER_SHA: undefined }],
  ["invalid runner SHA", { PLATFORM_LIVE_QA_RUNNER_SHA: "bad" }],
  ["missing marker SHA", { PLATFORM_LIVE_QA_MARKER_SHA256: undefined }],
  ["invalid marker SHA", { PLATFORM_LIVE_QA_MARKER_SHA256: "bad" }],
]) {
  assert.equal(
    bindingsFromEnvironment({ ...bindingEnv, ...bindingPatch }),
    null,
    `${label} is rejected for the public gate`,
  );
}

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "live-count-contract-"));
try {
  fs.chmodSync(temporary, 0o711);
  const gate = path.join(temporary, "public-live-qa.a1b2c3d4");
  fs.mkdirSync(gate, { mode: 0o700 });
  fs.mkdirSync(path.join(gate, "test-results"), { mode: 0o700 });
  const writeBinding = { ...bindingsFromEnvironment(bindingEnv), gate };
  const reportPath = path.join(gate, "test-results", "live-counts-v1.json");
  const rootMetadata = fs.lstatSync(temporary);
  if (rootMetadata.uid !== 0) {
    assert.equal(
      writeBoundedSummary(writeBinding, summary, { gateRoot: temporary }),
      false,
      "a non-root-owned gate root is rejected",
    );
    assert.equal(fs.existsSync(reportPath), false);
  }

  const publicNativeLstatSync = fs.lstatSync;
  try {
    if (rootMetadata.uid !== 0) {
      fs.lstatSync = function lstatWithRootOwnedGate(target, ...args) {
        const metadata = publicNativeLstatSync.call(fs, target, ...args);
        if (target !== temporary) return metadata;
        return new Proxy(metadata, {
          get(value, key, receiver) {
            return key === "uid" ? 0 : Reflect.get(value, key, receiver);
          },
        });
      };
    }
    assert.equal(writeBoundedSummary(writeBinding, summary, { gateRoot: temporary }), true);
    assert.equal(writeBoundedSummary(writeBinding, summary, { gateRoot: temporary }), false, "O_EXCL rejects replacement");
  } finally {
    fs.lstatSync = publicNativeLstatSync;
  }
  const metadata = fs.lstatSync(reportPath);
  assert.equal(metadata.isFile(), true);
  assert.equal(metadata.nlink, 1);
  assert.equal(metadata.mode & 0o777, 0o600);
  const reportBytes = fs.readFileSync(reportPath);
  assert.ok(reportBytes.length <= 1024);
  const report = JSON.parse(reportBytes.toString("ascii"));
  assert.equal(report.logical_total, 8);
  assert.equal(report.logical_pass + report.logical_fail + report.logical_expected_fail + report.logical_flaky + report.logical_skip + report.logical_interrupted, 8);
  assert.equal(report.attempt_total, 6);
  assert.equal(reportBytes.includes(Buffer.from("private-playwright-id")), false);
  assert.equal(reportBytes.includes(Buffer.from("private title")), false);
  assert.equal(reportBytes.includes(Buffer.from("private error")), false);
  assert.deepEqual(Object.keys(report).sort(), [
    "app_sha", "attempt_fail", "attempt_interrupted", "attempt_pass",
    "attempt_skip", "attempt_timedout", "attempt_total", "logical_fail",
    "logical_expected_fail", "logical_flaky", "logical_interrupted", "logical_pass", "logical_skip",
    "logical_total", "marker_sha256", "run_status", "schema", "source_sha",
  ].sort());

  const userGate = path.join(temporary, "live-user-qa.Abc123");
  fs.mkdirSync(userGate, { mode: 0o700 });
  fs.mkdirSync(path.join(userGate, "test-results"), { mode: 0o700 });
  const userWriteBinding = {
    ...bindingsFromEnvironment({
      ...userBindingEnv,
      PLATFORM_QA_BROWSER_GATE_DIR: "/run/oldsparky-liveqa/live-user-qa.Abc123",
      PLATFORM_LIVE_USER_QA_SESSIONS: "/run/oldsparky-liveqa/live-user-qa.Abc123/browser-sessions.json",
    }),
    gate: userGate,
  };
  const userReportPath = path.join(userGate, "test-results", "live-counts-v1.json");
  const nativeLstatSync = fs.lstatSync;
  try {
    if (rootMetadata.uid !== 0) {
      fs.lstatSync = function lstatWithRootOwnedGate(target, ...args) {
        const metadata = nativeLstatSync.call(fs, target, ...args);
        if (target !== temporary) return metadata;
        return new Proxy(metadata, {
          get(value, key, receiver) {
            return key === "uid" ? 0 : Reflect.get(value, key, receiver);
          },
        });
      };
    }
    assert.equal(writeBoundedSummary(userWriteBinding, liveUserSummary, { gateRoot: temporary }), true);
    assert.equal(writeBoundedSummary(userWriteBinding, summary, { gateRoot: temporary }), false, "live-user report also rejects replacement");
  } finally {
    fs.lstatSync = nativeLstatSync;
  }
  const userReport = JSON.parse(fs.readFileSync(userReportPath, "ascii"));
  assert.equal(userReport.app_sha, bindingEnv.PLATFORM_LIVE_QA_TARGET_SHA);
  assert.equal(userReport.source_sha, bindingEnv.PLATFORM_LIVE_QA_RUNNER_SHA);
  assert.equal(userReport.marker_sha256, bindingEnv.PLATFORM_LIVE_QA_MARKER_SHA256);
  assert.equal(userReport.logical_total, 1);
  assert.equal(userReport.logical_pass, 1);
  assert.equal(userReport.attempt_total, 1);
  assert.equal(userReport.attempt_pass, 1);
  assert.equal(userReport.run_status, "passed");
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}

function diagnosticFixture(root, suffix = "d1a2b3c4") {
  const gateRoot = path.join(root, "gate-root");
  const gate = path.join(gateRoot, `public-live-qa.${suffix}`);
  const results = path.join(gate, "test-results");
  const sourceRoot = path.join(root, "source");
  const spec = path.join(sourceRoot, "tests", "smoke", "live-launch.spec.ts");
  const helper = path.join(sourceRoot, "tests", "support", "live-qa-sandbox.ts");
  fs.mkdirSync(results, { recursive: true, mode: 0o700 });
  fs.chmodSync(gateRoot, 0o711);
  fs.mkdirSync(path.dirname(spec), { recursive: true, mode: 0o755 });
  fs.mkdirSync(path.dirname(helper), { recursive: true, mode: 0o755 });
  fs.writeFileSync(spec, 'test("declaration", async () => {\n  await helper();\n});\n');
  fs.writeFileSync(helper, 'async function check() {\n  await expect.poll(readSnapshot).toEqual({ ok: true });\n}\n');
  return {
    gateRoot,
    gate,
    results,
    sourceRoot,
    spec,
    helper,
    binding: {
      gate,
      source_sha: "b".repeat(40),
      app_sha: "a".repeat(40),
      marker_sha256: "c".repeat(64),
    },
  };
}

function withRootOwnedGate(gateRoot, run) {
  const originalLstat = fs.lstatSync;
  try {
    if (process.getuid() !== 0) {
      fs.lstatSync = function lstatWithRootOwnedGate(target, ...args) {
        const metadata = originalLstat.call(fs, target, ...args);
        if (target !== gateRoot) return metadata;
        return new Proxy(metadata, {
          get(value, key, receiver) {
            return key === "uid" ? 0 : Reflect.get(value, key, receiver);
          },
        });
      };
    }
    return run();
  } finally {
    fs.lstatSync = originalLstat;
  }
}

function failureTest(
  fixture,
  { title = publicTitles[6], project = "live-desktop", errorLocation, attachments, results } = {},
) {
  return {
    title,
    expectedStatus: "passed",
    parent: { project: () => ({ name: project }) },
    results: results ?? [{
      status: "failed",
      errors: [{ message: "private assertion text", location: errorLocation }],
      attachments,
    }],
    outcome: () => "unexpected",
    location: { file: fixture.spec, line: 1, column: 1 },
  };
}

function publicPlanWithFailure(failure) {
  const project = failure.parent.project().name;
  let matches = 0;
  const selected = publicTests.map((testCase) => {
    if (testCase.title !== failure.title || testCase.parent.project().name !== project) {
      return testCase;
    }
    matches += 1;
    return failure;
  });
  assert.equal(matches, 1, "a failure must belong to one planned public case");
  return selected;
}

const failureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "live-failure-contract-"));
try {
  const successFixture = diagnosticFixture(failureRoot, "c1a2b3c4");
  const successfulSummary = summarizeRun(publicSuite, "passed", { publicGate: true });
  assert.equal(successfulSummary.run_status, "passed");
  assert.equal(withRootOwnedGate(successFixture.gateRoot, () => writeBoundedFailureSummary(
    successFixture.binding,
    publicSuite,
    successfulSummary,
    { gateRoot: successFixture.gateRoot, sourceRoot: successFixture.sourceRoot },
  )), false, "a successful public suite does not create private failure details");
  assert.equal(
    fs.existsSync(path.join(successFixture.results, "live-failures-v2.json")),
    false,
  );

  const fixture = diagnosticFixture(failureRoot);
  const failure = failureTest(fixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    errorLocation: { file: fixture.helper, line: 2, column: 3 },
    attachments: [pageErrorAttachment],
  });
  const failurePlan = publicPlanWithFailure(failure);
  const failedSummary = summarizeRun(
    { allTests: () => failurePlan },
    "failed",
    { publicGate: true },
  );
  assert.equal(failedSummary.run_status, "failed");
  assert.equal(withRootOwnedGate(fixture.gateRoot, () => writeBoundedFailureSummary(
    fixture.binding,
    { allTests: () => failurePlan },
    failedSummary,
    { gateRoot: fixture.gateRoot, sourceRoot: fixture.sourceRoot },
  )), true, "a shared support-helper assertion is retained as a closed failure record");
  const reportPath = path.join(fixture.results, "live-failures-v2.json");
  const reportMetadata = fs.lstatSync(reportPath);
  const reportBytes = fs.readFileSync(reportPath);
  const report = JSON.parse(reportBytes.toString("ascii"));
  assert.equal(reportMetadata.isFile(), true);
  assert.equal(reportMetadata.nlink, 1);
  assert.equal(reportMetadata.mode & 0o777, 0o600);
  assert.ok(reportBytes.length <= 16 * 1024);
  assert.deepEqual(Object.keys(report).sort(), [
    "app_sha", "failures", "gate", "marker_sha256", "run_status", "schema", "source_sha",
  ].sort());
  assert.equal(report.schema, 2);
  assert.deepEqual(report.failures, [{
    id: "image_csp_hosts",
    project: "live-webkit-mobile",
    status: "failed",
    attempt: 1,
    source_file: "tests/support/live-qa-sandbox.ts",
    source_line: 2,
    source_column: 3,
    source_kind: "assertion",
    page_error_state: "captured",
    page_errors_truncated: false,
    page_errors: [["TypeError", "patch", "abcdefghijklm.js", 12, 34]],
  }]);
  assert.equal(reportBytes.includes(Buffer.from("private assertion text")), false);
  assert.equal(reportBytes.includes(Buffer.from("private-playwright-id")), false);
  assert.equal(reportBytes.includes(Buffer.from("private title")), false);
  assert.equal(reportBytes.includes(Buffer.from(failureRoot)), false);
  assert.equal(reportBytes.includes(Buffer.from("abcdefghijklm.js:12:34")), false);

  const unavailableFixture = diagnosticFixture(failureRoot, "c2a2b3c4");
  const unavailable = failureTest(unavailableFixture, {
    errorLocation: { file: unavailableFixture.helper, line: 2, column: 3 },
  });
  const unavailablePlan = publicPlanWithFailure(unavailable);
  assert.equal(withRootOwnedGate(unavailableFixture.gateRoot, () => writeBoundedFailureSummary(
    unavailableFixture.binding,
    { allTests: () => unavailablePlan },
    failedSummary,
    { gateRoot: unavailableFixture.gateRoot, sourceRoot: unavailableFixture.sourceRoot },
  )), true);
  const unavailableReport = JSON.parse(fs.readFileSync(
    path.join(unavailableFixture.results, "live-failures-v2.json"),
    "ascii",
  ));
  assert.deepEqual(
    [
      unavailableReport.failures[0].page_error_state,
      unavailableReport.failures[0].page_errors_truncated,
      unavailableReport.failures[0].page_errors,
    ],
    ["unavailable", false, []],
    "a missing attachment is unavailable, not an empty captured pageerror set",
  );

  const absentFixture = diagnosticFixture(failureRoot, "c3a2b3c4");
  const explicitAbsence = failureTest(absentFixture, {
    errorLocation: { file: absentFixture.helper, line: 2, column: 3 },
    attachments: [createPageErrorAttachment({
      pageUrl: "https://old-sparky.com/",
      errors: [],
      successfulScriptResponses: [],
    })],
  });
  const absentPlan = publicPlanWithFailure(explicitAbsence);
  assert.equal(withRootOwnedGate(absentFixture.gateRoot, () => writeBoundedFailureSummary(
    absentFixture.binding,
    { allTests: () => absentPlan },
    failedSummary,
    { gateRoot: absentFixture.gateRoot, sourceRoot: absentFixture.sourceRoot },
  )), true);
  const absentReport = JSON.parse(fs.readFileSync(
    path.join(absentFixture.results, "live-failures-v2.json"),
    "ascii",
  ));
  assert.deepEqual(
    [
      absentReport.failures[0].page_error_state,
      absentReport.failures[0].page_errors_truncated,
      absentReport.failures[0].page_errors,
    ],
    ["absent", false, []],
    "an explicit empty attachment records an observed absence",
  );

  const invalidEvidenceCases = [
    ["wrong content type", [{ ...pageErrorAttachment, contentType: "text/plain" }]],
    ["path instead of body", [{ name: pageErrorAttachment.name, contentType: "application/json", path: "/private/file" }]],
    ["body and path", [{ ...pageErrorAttachment, path: "/private/file" }]],
    ["duplicate attachment", [pageErrorAttachment, pageErrorAttachment]],
    ["oversize attachment", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.alloc(2049, 0x61),
    }]],
    ["malformed JSON", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.from("{", "ascii"),
    }]],
    ["unsafe basename", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({
        schema: 1,
        truncated: false,
        events: [{
          class: "TypeError",
          route: "patch",
          asset_basename: "../../private.js",
          line: 12,
          column: 34,
        }],
      }), "ascii"),
    }]],
    ["non-string class", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({
        schema: 1,
        truncated: false,
        events: [[{ value: "TypeError" }, "patch", null, null, null]],
      }), "ascii"),
    }]],
    ["non-string route", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({
        schema: 1,
        truncated: false,
        events: [["TypeError", [], null, null, null]],
      }), "ascii"),
    }]],
    ["malformed tuple length", [{
      name: pageErrorAttachment.name,
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({
        schema: 1,
        truncated: false,
        events: [["TypeError", "patch", null, null]],
      }), "ascii"),
    }]],
  ];
  for (const [index, [label, attachments]] of invalidEvidenceCases.entries()) {
    const invalidFixture = diagnosticFixture(
      failureRoot,
      `d${index.toString().padStart(2, "0")}2b3c4`,
    );
    const invalid = failureTest(invalidFixture, {
      errorLocation: { file: invalidFixture.helper, line: 2, column: 3 },
      attachments,
    });
    const invalidPlan = publicPlanWithFailure(invalid);
    assert.equal(withRootOwnedGate(invalidFixture.gateRoot, () => writeBoundedFailureSummary(
      invalidFixture.binding,
      { allTests: () => invalidPlan },
      failedSummary,
      { gateRoot: invalidFixture.gateRoot, sourceRoot: invalidFixture.sourceRoot },
    )), true, `${label} does not suppress the original failed QA record`);
    const invalidReport = JSON.parse(fs.readFileSync(
      path.join(invalidFixture.results, "live-failures-v2.json"),
      "ascii",
    ));
    assert.deepEqual(
      [
        invalidReport.failures[0].page_error_state,
        invalidReport.failures[0].page_errors_truncated,
        invalidReport.failures[0].page_errors,
      ],
      ["unavailable", false, []],
      `${label} fails closed without changing failure status`,
    );
  }

  const malformedFixture = diagnosticFixture(failureRoot, "e1a2b3c4");
  const malformed = failureTest(malformedFixture, {
    errorLocation: { file: "../../etc/passwd", line: 2, column: 3 },
  });
  const malformedPlan = publicPlanWithFailure(malformed);
  assert.equal(withRootOwnedGate(malformedFixture.gateRoot, () => writeBoundedFailureSummary(
    malformedFixture.binding,
    { allTests: () => malformedPlan },
    failedSummary,
    { gateRoot: malformedFixture.gateRoot, sourceRoot: malformedFixture.sourceRoot },
  )), true);
  const fallbackReport = JSON.parse(
    fs.readFileSync(path.join(malformedFixture.results, "live-failures-v2.json"), "ascii"),
  );
  assert.equal(fallbackReport.failures[0].source_file, "tests/smoke/live-launch.spec.ts");
  assert.equal(fallbackReport.failures[0].source_kind, "declaration");
  assert.equal(fallbackReport.failures[0].source_line, 1);

  const unknownFixture = diagnosticFixture(failureRoot, "f1a2b3c4");
  const unknown = failureTest(unknownFixture, { title: "arbitrary private test title" });
  const unknownPlan = [...publicTests];
  unknownPlan[0] = unknown;
  assert.equal(withRootOwnedGate(unknownFixture.gateRoot, () => writeBoundedFailureSummary(
    unknownFixture.binding,
    { allTests: () => unknownPlan },
    failedSummary,
    { gateRoot: unknownFixture.gateRoot, sourceRoot: unknownFixture.sourceRoot },
  )), false, "a test outside the complete fixed public manifest is not serialized");
  assert.equal(fs.existsSync(path.join(unknownFixture.results, "live-failures-v2.json")), false);

  const symlinkFixture = diagnosticFixture(failureRoot, "a1a2b3c4");
  const symlinkPath = path.join(symlinkFixture.results, "live-failures-v2.json");
  fs.symlinkSync("/dev/null", symlinkPath);
  const symlinkCase = failureTest(symlinkFixture, {
    errorLocation: { file: symlinkFixture.helper, line: 2, column: 3 },
  });
  assert.equal(withRootOwnedGate(symlinkFixture.gateRoot, () => writeBoundedFailureSummary(
    symlinkFixture.binding,
    { allTests: () => [symlinkCase] },
    failedSummary,
    { gateRoot: symlinkFixture.gateRoot, sourceRoot: symlinkFixture.sourceRoot },
  )), false, "a preexisting symlink failure report is never followed or replaced");
  assert.equal(fs.lstatSync(symlinkPath).isSymbolicLink(), true);

  const sizeFixture = diagnosticFixture(failureRoot, "b1a2b3c4");
  const maxError = new AggregateError([], "private max-size error");
  maxError.stack = `AggregateError: private max-size error\n    at render (https://old-sparky.com/_next/static/chunks/turbopack-abcdefghijklm.js:1000000:1000000)`;
  const maxAttachment = createPageErrorAttachment({
    pageUrl: "https://old-sparky.com/tournaments",
    errors: [maxError, maxError, maxError, maxError],
    successfulScriptResponses: [{
      url: "https://old-sparky.com/_next/static/chunks/turbopack-abcdefghijklm.js",
      status: 200,
      resourceType: "script",
    }],
  });
  assert.ok(maxAttachment);
  fs.writeFileSync(
    sizeFixture.helper,
    `${"\n".repeat(9_999)}expect(${"x".repeat(9_993)})\n`,
    "utf8",
  );
  const maxSourceLocation = {
    file: sizeFixture.helper,
    line: 10_000,
    column: 10_000,
  };
  const requiredSkips = new Set(
    publicTests
      .filter((testCase) => testCase.outcome() === "skipped")
      .slice(0, 6)
      .map((testCase) => `${testCase.parent.project().name}:${testCase.title}`),
  );
  const maxFailedAttempts = publicTests.map((testCase) => {
    const project = testCase.parent.project().name;
    const key = `${project}:${testCase.title}`;
    const failureResult = {
      status: "interrupted",
      errors: [{
        message: "private assertion text",
        location: maxSourceLocation,
      }],
      attachments: [maxAttachment],
    };
    if (!requiredSkips.has(key)) {
      return failureTest(sizeFixture, {
        title: testCase.title,
        project,
        errorLocation: maxSourceLocation,
        attachments: [maxAttachment],
        results: [failureResult],
      });
    }
    const terminalSkip = {
      ...testCase.results.at(-1),
      status: "skipped",
    };
    return {
      title: testCase.title,
      expectedStatus: "skipped",
      parent: testCase.parent,
      results: [failureResult, terminalSkip],
      outcome: () => "skipped",
      location: { file: sizeFixture.spec, line: 1, column: 1 },
    };
  });
  const fullFailureSummary = summarizeRun(
    { allTests: () => maxFailedAttempts },
    "failed",
    { publicGate: true },
  );
  assert.equal(fullFailureSummary.attempts.interrupted, 54);
  assert.equal(fullFailureSummary.logical.skip, 6);
  assert.equal(withRootOwnedGate(sizeFixture.gateRoot, () => writeBoundedFailureSummary(
    sizeFixture.binding,
    { allTests: () => maxFailedAttempts },
    fullFailureSummary,
    { gateRoot: sizeFixture.gateRoot, sourceRoot: sizeFixture.sourceRoot },
  )), true, "all 54 validated failed attempts survive the bounded report");
  const maximumReportBytes = fs.readFileSync(
    path.join(sizeFixture.results, "live-failures-v2.json"),
  );
  const maximumReport = JSON.parse(maximumReportBytes.toString("ascii"));
  assert.ok(maximumReportBytes.length <= 16 * 1024);
  assert.ok(maximumReportBytes.length <= 16 * 1024 - 256);
  assert.equal(maximumReport.failures.length, 54);
  assert.ok(maximumReport.failures.every((failure) => failure.status === "interrupted"));
  assert.ok(maximumReport.failures.every((failure) => (
    failure.source_line === 10_000 && failure.source_column === 10_000
  )));
  assert.ok(maximumReport.failures.every((failure) => failure.page_errors_truncated));
  assert.ok(maximumReport.failures.some((failure) => failure.page_errors.length === 0));
  assert.ok(maximumReport.failures.some((failure) => failure.page_errors.some((event) => (
    event[0] === "AggregateError"
      && event[1] === "tournaments"
      && event[2] === "turbopack-abcdefghijklm.js"
      && event[3] === 1_000_000
      && event[4] === 1_000_000
  ))));
  assert.equal(maximumReportBytes.includes(Buffer.from("private max-size error")), false);
  assert.equal(maximumReportBytes.includes(Buffer.from("https://old-sparky.com")), false);
  assert.equal(
    maximumReportBytes.includes(Buffer.from("turbopack-abcdefghijklm.js:1000000:1000000")),
    false,
  );
  assert.ok(maximumReport.failures.every((failure) => (
    failure.page_error_state === "captured" ? failure.page_errors.length >= 1 : (
      failure.page_error_state === "unavailable" && failure.page_errors.length === 0
    )
  )));
  assert.ok(maximumReport.failures.every((failure) => failure.id && failure.project && failure.status));
} finally {
  fs.rmSync(failureRoot, { recursive: true, force: true });
}

const enginePreflightRoot = fs.mkdtempSync(
  path.join(os.tmpdir(), "live-engine-preflight-contract-"),
);
try {
  const fakePackage = path.join(
    enginePreflightRoot,
    "node_modules",
    "@playwright",
    "test",
  );
  fs.mkdirSync(fakePackage, { recursive: true });
  fs.writeFileSync(path.join(fakePackage, "index.js"), `
    const fs = require("node:fs");
    function record(event) {
      fs.appendFileSync(process.env.PREFLIGHT_EVENTS, JSON.stringify(event) + "\\n");
    }
    function engine(name) {
      return {
        launch: async (options) => {
          record([name, "launch", options]);
          return {
            newContext: async (options) => {
              record([name, "context", options]);
              return {
                newPage: async () => {
                  record([name, "newPage"]);
                  if (process.env.FAIL_ENGINE === name) throw new Error("private engine failure");
                  return {
                    goto: async (url) => record([name, "goto", url]),
                    url: () => "about:blank",
                  };
                },
                close: async () => {
                  record([name, "context-close"]);
                  if (process.env.FAIL_CONTEXT_CLOSE === name) {
                    throw new Error("private context close failure");
                  }
                },
              };
            },
            close: async () => record([name, "browser-close"]),
          };
        },
      };
    }
    module.exports = {
      chromium: engine("chromium"),
      webkit: engine("webkit"),
      devices: {
        "Desktop Chrome": { deviceScaleFactor: 1, userAgent: "desktop" },
        "iPhone 13": { deviceScaleFactor: 3, isMobile: true, hasTouch: true, userAgent: "iphone" },
      },
    };
  `, "utf8");
  // Resolve the fake Playwright package from this disposable fixture. If the
  // repository helper path were used directly, Node would prefer an installed
  // platform_web/node_modules package over NODE_PATH and launch real browsers.
  const preflightScript = path.join(
    enginePreflightRoot,
    "live-engine-preflight.cjs",
  );
  const sourcePreflightScript = path.resolve(__dirname, "live-engine-preflight.cjs");
  fs.copyFileSync(sourcePreflightScript, preflightScript);
  assert.deepEqual(
    fs.readFileSync(preflightScript),
    fs.readFileSync(sourcePreflightScript),
    "the disposable child must execute the checked-in helper bytes",
  );
  const eventsPath = path.join(enginePreflightRoot, "events.jsonl");
  const runPreflight = (failEngine = "", failContextClose = "") => spawnSync(
    process.execPath,
    [preflightScript],
    {
      encoding: "utf8",
      cwd: enginePreflightRoot,
      env: {
        NODE_PATH: path.join(enginePreflightRoot, "node_modules"),
        PREFLIGHT_EVENTS: eventsPath,
        ...(failEngine ? { FAIL_ENGINE: failEngine } : {}),
        ...(failContextClose ? { FAIL_CONTEXT_CLOSE: failContextClose } : {}),
      },
      timeout: 5_000,
    },
  );

  const passingPreflight = runPreflight();
  assert.equal(passingPreflight.status, 0, passingPreflight.stderr);
  assert.equal(
    passingPreflight.stdout,
    "LIVE_QA_ENGINE_PREFLIGHT_READY schema=1\n",
  );
  const passingEvents = fs.readFileSync(eventsPath, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.deepEqual(passingEvents.map(([engineName, action]) => [engineName, action]), [
    ["chromium", "launch"], ["chromium", "context"], ["chromium", "newPage"],
    ["chromium", "goto"], ["chromium", "context-close"], ["chromium", "browser-close"],
    ["webkit", "launch"], ["webkit", "context"], ["webkit", "newPage"],
    ["webkit", "goto"], ["webkit", "context-close"], ["webkit", "browser-close"],
  ]);
  assert.deepEqual(passingEvents[0][2], { headless: true, chromiumSandbox: true });
  assert.deepEqual(passingEvents[1][2], {
    deviceScaleFactor: 1,
    userAgent: "desktop",
    viewport: { width: 1440, height: 1000 },
  });
  assert.deepEqual(passingEvents[6][2], { headless: true, chromiumSandbox: true });
  assert.equal(passingEvents[7][2].isMobile, true);
  assert.equal(passingEvents[7][2].hasTouch, true);
  assert.ok(passingEvents.filter((event) => event[1] === "goto").every(
    (event) => event[2] === "about:blank",
  ));

  fs.writeFileSync(eventsPath, "", "utf8");
  const failedPreflight = runPreflight("webkit");
  assert.equal(failedPreflight.status, 1);
  assert.equal(failedPreflight.stdout, "");
  const failedEvents = fs.readFileSync(eventsPath, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.ok(failedEvents.some((event) => event[0] === "webkit" && event[1] === "context-close"));
  assert.ok(failedEvents.some((event) => event[0] === "webkit" && event[1] === "browser-close"));

  fs.writeFileSync(eventsPath, "", "utf8");
  const failedContextClose = runPreflight("", "chromium");
  assert.equal(failedContextClose.status, 1);
  assert.equal(failedContextClose.stdout, "");
  const closeFailureEvents = fs.readFileSync(eventsPath, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.ok(closeFailureEvents.some(
    (event) => event[0] === "chromium" && event[1] === "context-close",
  ));
  assert.ok(closeFailureEvents.some(
    (event) => event[0] === "chromium" && event[1] === "browser-close",
  ));
} finally {
  fs.rmSync(enginePreflightRoot, { recursive: true, force: true });
}

process.stdout.write("live-count-reporter-contract: PASS\n");
