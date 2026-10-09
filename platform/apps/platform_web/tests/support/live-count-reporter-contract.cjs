"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This contract runs as CommonJS. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  bindingsFromEnvironment,
  summarizeRun,
  writeBoundedSummary,
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
    assert.equal(writeBoundedSummary(userWriteBinding, summary, { gateRoot: temporary }), true);
    assert.equal(writeBoundedSummary(userWriteBinding, summary, { gateRoot: temporary }), false, "live-user report also rejects replacement");
  } finally {
    fs.lstatSync = nativeLstatSync;
  }
  const userReport = JSON.parse(fs.readFileSync(userReportPath, "ascii"));
  assert.equal(userReport.app_sha, bindingEnv.PLATFORM_LIVE_QA_TARGET_SHA);
  assert.equal(userReport.source_sha, bindingEnv.PLATFORM_LIVE_QA_RUNNER_SHA);
  assert.equal(userReport.marker_sha256, bindingEnv.PLATFORM_LIVE_QA_MARKER_SHA256);
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}

process.stdout.write("live-count-reporter-contract: PASS\n");
