"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This helper is CommonJS. */

const fs = require("node:fs");
const path = require("node:path");

const MAX_TESTS = 4096;
const MAX_REPORT_BYTES = 1024;
const MAX_ATTEMPTS = 32768;
const GATE_ROOT = "/run/oldsparky-liveqa";
const RUN_STATUSES = new Set(["passed", "failed", "timedout", "interrupted"]);
const ATTEMPT_STATUSES = new Set([
  "passed",
  "failed",
  "timedOut",
  "skipped",
  "interrupted",
]);
const SHA40 = /^[0-9a-f]{40}$/u;
const SHA64 = /^[0-9a-f]{64}$/u;

function unavailable() {
  return null;
}

function summarizeRun(suite, runStatus) {
  if (!RUN_STATUSES.has(runStatus)) return unavailable();
  if (!suite || typeof suite.allTests !== "function") return unavailable();
  const tests = suite.allTests();
  if (!Array.isArray(tests) || tests.length > MAX_TESTS) return unavailable();

  const logical = {
    total: tests.length,
    pass: 0,
    fail: 0,
    expected_fail: 0,
    flaky: 0,
    skip: 0,
    interrupted: 0,
  };
  const attempts = {
    total: 0,
    pass: 0,
    fail: 0,
    skip: 0,
    interrupted: 0,
    timedout: 0,
  };

  for (const test of tests) {
    if (!test || typeof test.outcome !== "function" || !Array.isArray(test.results)) {
      return unavailable();
    }
    for (const result of test.results) {
      if (attempts.total >= MAX_ATTEMPTS) return unavailable();
      if (!result) return unavailable();
      const status = result.status;
      if (!ATTEMPT_STATUSES.has(status)) return unavailable();
      attempts.total += 1;
      if (status === "passed") attempts.pass += 1;
      else if (status === "failed") attempts.fail += 1;
      else if (status === "timedOut") attempts.timedout += 1;
      else if (status === "skipped") attempts.skip += 1;
      else attempts.interrupted += 1;
    }

    const last = test.results.at(-1);
    if (last?.status === "interrupted") {
      logical.interrupted += 1;
      continue;
    }

    const outcome = test.outcome();
    if (!last && outcome !== "skipped") logical.interrupted += 1;
    else if (outcome === "skipped") logical.skip += 1;
    else if (outcome === "flaky") logical.flaky += 1;
    else if (outcome === "unexpected") logical.fail += 1;
    else if (outcome === "expected" && test.expectedStatus === "failed") logical.expected_fail += 1;
    else if (outcome === "expected" && test.expectedStatus === "passed") logical.pass += 1;
    else return unavailable();
  }

  if (
    logical.pass + logical.fail + logical.expected_fail + logical.flaky
      + logical.skip + logical.interrupted
      !== logical.total
    || attempts.total > MAX_ATTEMPTS
    || attempts.pass + attempts.fail + attempts.skip + attempts.interrupted
      + attempts.timedout !== attempts.total
  ) {
    return unavailable();
  }
  return { run_status: runStatus, logical, attempts };
}

function bindingsFromEnvironment(env = process.env) {
  const gate = env.PLATFORM_QA_BROWSER_GATE_DIR;
  const appSha = env.PLATFORM_LIVE_QA_TARGET_SHA;
  const runnerSha = env.PLATFORM_LIVE_QA_RUNNER_SHA;
  const markerSha = env.PLATFORM_LIVE_QA_MARKER_SHA256;
  if (
    typeof gate !== "string"
    || !/^\/run\/oldsparky-liveqa\/public-live-qa\.[a-z0-9_]{8}$/u.test(gate)
    || !SHA40.test(appSha ?? "")
    || !SHA40.test(runnerSha ?? "")
    || !SHA64.test(markerSha ?? "")
  ) {
    return unavailable();
  }
  return { gate, app_sha: appSha, source_sha: runnerSha, marker_sha256: markerSha };
}

function writeBoundedSummary(binding, summary, { gateRoot = GATE_ROOT } = {}) {
  if (!binding || !summary) return false;
  if (
    path.dirname(binding.gate) !== gateRoot
    || !/^public-live-qa\.[a-z0-9_]{8}$/u.test(path.basename(binding.gate))
  ) return false;
  let gateMetadata;
  let resultsMetadata;
  let rootMetadata;
  try {
    rootMetadata = fs.lstatSync(gateRoot);
    gateMetadata = fs.lstatSync(binding.gate);
    resultsMetadata = fs.lstatSync(path.join(binding.gate, "test-results"));
  } catch {
    return false;
  }
  if (
    !rootMetadata.isDirectory()
    || rootMetadata.isSymbolicLink()
    || rootMetadata.uid !== 0
    || (rootMetadata.mode & 0o777) !== 0o711
    || fs.realpathSync(gateRoot) !== gateRoot
    ||
    !gateMetadata.isDirectory()
    || gateMetadata.isSymbolicLink()
    || gateMetadata.uid !== process.geteuid()
    || (gateMetadata.mode & 0o777) !== 0o700
    || !resultsMetadata.isDirectory()
    || resultsMetadata.isSymbolicLink()
    || resultsMetadata.uid !== process.geteuid()
    || (resultsMetadata.mode & 0o777) !== 0o700
  ) return false;
  const output = path.join(binding.gate, "test-results", "live-counts-v1.json");
  const payload = {
    schema: 1,
    source_sha: binding.source_sha,
    app_sha: binding.app_sha,
    marker_sha256: binding.marker_sha256,
    run_status: summary.run_status,
    logical_total: summary.logical.total,
    logical_pass: summary.logical.pass,
    logical_fail: summary.logical.fail,
    logical_expected_fail: summary.logical.expected_fail,
    logical_flaky: summary.logical.flaky,
    logical_skip: summary.logical.skip,
    logical_interrupted: summary.logical.interrupted,
    attempt_total: summary.attempts.total,
    attempt_pass: summary.attempts.pass,
    attempt_fail: summary.attempts.fail,
    attempt_skip: summary.attempts.skip,
    attempt_interrupted: summary.attempts.interrupted,
    attempt_timedout: summary.attempts.timedout,
  };
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
  if (bytes.length > MAX_REPORT_BYTES) return false;
  let fd;
  try {
    fd = fs.openSync(
      output,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL
        | (fs.constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    const metadata = fs.fstatSync(fd);
    if (
      !metadata.isFile()
      || metadata.nlink !== 1
      || metadata.uid !== process.geteuid()
      || (metadata.mode & 0o777) !== 0o600
    ) {
      return false;
    }
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
    return true;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

class LiveCountReporter {
  onBegin(_config, suite) {
    this.suite = suite;
  }

  onEnd(result) {
    const binding = bindingsFromEnvironment();
    const summary = summarizeRun(this.suite, result?.status);
    if (!writeBoundedSummary(binding, summary)) return { status: "failed" };
    return undefined;
  }

  printsToStdio() {
    return false;
  }
}

module.exports = LiveCountReporter;
module.exports.summarizeRun = summarizeRun;
module.exports.bindingsFromEnvironment = bindingsFromEnvironment;
module.exports.writeBoundedSummary = writeBoundedSummary;
