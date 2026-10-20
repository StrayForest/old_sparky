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

function failureTest(fixture, { title = publicTitles[6], project = "live-desktop", errorLocation, results } = {}) {
  return {
    title,
    expectedStatus: "passed",
    parent: { project: () => ({ name: project }) },
    results: results ?? [{
      status: "failed",
      errors: [{ message: "private assertion text", location: errorLocation }],
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
    fs.existsSync(path.join(successFixture.results, "live-failures-v1.json")),
    false,
  );

  const fixture = diagnosticFixture(failureRoot);
  const failure = failureTest(fixture, {
    errorLocation: { file: fixture.helper, line: 2, column: 3 },
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
  const reportPath = path.join(fixture.results, "live-failures-v1.json");
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
  assert.deepEqual(report.failures, [{
    id: "chromium_sandbox",
    project: "live-desktop",
    status: "failed",
    attempt: 1,
    source_file: "tests/support/live-qa-sandbox.ts",
    source_line: 2,
    source_column: 3,
    source_kind: "assertion",
  }]);
  assert.equal(reportBytes.includes(Buffer.from("private assertion text")), false);
  assert.equal(reportBytes.includes(Buffer.from("private-playwright-id")), false);
  assert.equal(reportBytes.includes(Buffer.from("private title")), false);
  assert.equal(reportBytes.includes(Buffer.from(failureRoot)), false);

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
    fs.readFileSync(path.join(malformedFixture.results, "live-failures-v1.json"), "ascii"),
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
  assert.equal(fs.existsSync(path.join(unknownFixture.results, "live-failures-v1.json")), false);

  const symlinkFixture = diagnosticFixture(failureRoot, "a1a2b3c4");
  const symlinkPath = path.join(symlinkFixture.results, "live-failures-v1.json");
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
  const requiredSkips = new Set(
    publicTests
      .filter((testCase) => testCase.outcome() === "skipped")
      .slice(0, 6)
      .map((testCase) => `${testCase.parent.project().name}:${testCase.title}`),
  );
  const fullPlanFailures = publicTests.map((testCase) => {
    const key = `${testCase.parent.project().name}:${testCase.title}`;
    return requiredSkips.has(key) ? testCase : failureTest(sizeFixture, {
      title: testCase.title,
      project: testCase.parent.project().name,
      errorLocation: { file: sizeFixture.helper, line: 2, column: 3 },
    });
  });
  const fullFailureSummary = summarizeRun(
    { allTests: () => fullPlanFailures },
    "failed",
    { publicGate: true },
  );
  assert.equal(fullFailureSummary.attempts.fail, 48);
  assert.equal(fullFailureSummary.logical.skip, 6);
  assert.equal(withRootOwnedGate(sizeFixture.gateRoot, () => writeBoundedFailureSummary(
    sizeFixture.binding,
    { allTests: () => fullPlanFailures },
    fullFailureSummary,
    { gateRoot: sizeFixture.gateRoot, sourceRoot: sizeFixture.sourceRoot },
  )), true, "48 fixed attempt failures fit within the bounded report");
  assert.ok(fs.statSync(path.join(sizeFixture.results, "live-failures-v1.json")).size <= 16 * 1024);
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
  const preflightScript = path.resolve(
    __dirname,
    "live-engine-preflight.cjs",
  );
  const eventsPath = path.join(enginePreflightRoot, "events.jsonl");
  const runPreflight = (failEngine = "", failContextClose = "") => spawnSync(
    process.execPath,
    [preflightScript],
    {
      encoding: "utf8",
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
