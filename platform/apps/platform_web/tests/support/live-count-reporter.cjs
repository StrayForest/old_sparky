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
const PUBLIC_GATE_NAME = /^public-live-qa\.[a-z0-9_]{8}$/u;
const LIVE_USER_GATE_NAME = /^live-user-qa\.[A-Za-z0-9]{6}$/u;
const PUBLIC_LOGICAL_TOTAL = 54;
const EXPECTED_PUBLIC_SKIP_TYPE = "public-live-qa-expected-skip";
const PUBLIC_PROJECTS = new Set([
  "live-desktop",
  "live-mobile",
  "live-webkit-mobile",
]);
const PUBLIC_TEST_TITLES = new Set([
  ...[
    "/", "/info", "/tournaments", "/tournaments/new", "/profile/me", "/profile/lisalexy",
  ].map((route) => `live route ${route} renders without horizontal overflow`),
  "production Chromium live QA keeps its process sandbox enabled",
  "live CSP nonce is stable on soft navigation and rotates on hard reload",
  "local enforced CSP blocks negative inline and external probes",
  "live tournaments hub exposes a valid empty or populated list",
  "live 1920 catalog stays contained after every card asset loads",
  "live tournament detail and bracket routes render from the current public data",
  "live admin route is protected for anonymous users",
  "live home uses text-only tournament steps without overflow",
  "live public contact surfaces do not publish the support recipient",
  "live patch renders separate Urn and Rift objectives with source icons",
  "live Cloudflare Analytics is conditionally observed without widening CSP",
  "live image currentSrc inventory stays inside the exact CSP hosts",
]);
const EXPECTED_PUBLIC_SKIP_DESCRIPTIONS = new Map([
  ["production Chromium live QA keeps its process sandbox enabled", new Set([
    "chromium_only:live-webkit-mobile",
  ])],
  ["local enforced CSP blocks negative inline and external probes", new Set([
    "local_csp_disabled:live-desktop",
    "local_csp_disabled:live-mobile",
    "local_csp_disabled:live-webkit-mobile",
  ])],
  ["live 1920 catalog stays contained after every card asset loads", new Set([
    "desktop_only:live-mobile",
    "desktop_only:live-webkit-mobile",
    "empty_tournament_list:live-desktop",
  ])],
  ["live tournament detail and bracket routes render from the current public data", new Set([
    "empty_tournament_list:live-desktop",
    "empty_tournament_list:live-mobile",
    "empty_tournament_list:live-webkit-mobile",
  ])],
]);

function supportedGateName(name) {
  return PUBLIC_GATE_NAME.test(name) || LIVE_USER_GATE_NAME.test(name);
}

function unavailable() {
  return null;
}

function expectedPublicSkip(test) {
  if (!test || typeof test.title !== "string" || !Array.isArray(test.results)) {
    return false;
  }
  const result = test.results.at(-1);
  if (!result || !Array.isArray(result.annotations)) return false;
  const allowed = EXPECTED_PUBLIC_SKIP_DESCRIPTIONS.get(test.title);
  if (!allowed) return false;
  const annotations = result.annotations.filter(
    (annotation) => annotation?.type === EXPECTED_PUBLIC_SKIP_TYPE,
  );
  const project = typeof test.parent?.project === "function"
    ? test.parent.project()?.name
    : undefined;
  return annotations.length === 1
    && typeof project === "string"
    && allowed.has(annotations[0].description)
    && annotations[0].description.endsWith(`:${project}`);
}

function publicCoverageIsComplete(tests, logical) {
  if (
    tests.length !== PUBLIC_LOGICAL_TOTAL
    || logical.skip < 6
    || logical.skip > 10
  ) return false;
  const seen = new Set();
  for (const test of tests) {
    const project = typeof test.parent?.project === "function"
      ? test.parent.project()?.name
      : undefined;
    if (
      typeof test.title !== "string"
      || !PUBLIC_TEST_TITLES.has(test.title)
      || !PUBLIC_PROJECTS.has(project)
    ) return false;
    const key = `${project}:${test.title}`;
    if (seen.has(key)) return false;
    seen.add(key);
    if (test.outcome() === "skipped" && !expectedPublicSkip(test)) return false;
  }
  return seen.size === PUBLIC_LOGICAL_TOTAL;
}

function summarizeRun(suite, runStatus, { publicGate = false } = {}) {
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
  return {
    run_status: publicGate && !publicCoverageIsComplete(tests, logical)
      ? "failed"
      : runStatus,
    logical,
    attempts,
  };
}

function bindingsFromEnvironment(env = process.env) {
  const gate = env.PLATFORM_QA_BROWSER_GATE_DIR;
  const sessions = env.PLATFORM_LIVE_USER_QA_SESSIONS;
  const appSha = env.PLATFORM_LIVE_QA_TARGET_SHA;
  const runnerSha = env.PLATFORM_LIVE_QA_RUNNER_SHA;
  const markerSha = env.PLATFORM_LIVE_QA_MARKER_SHA256;
  const gateName = typeof gate === "string" ? path.basename(gate) : "";
  const publicGate = PUBLIC_GATE_NAME.test(gateName);
  const liveUserGate = LIVE_USER_GATE_NAME.test(gateName);
  if (
    typeof gate !== "string"
    || path.dirname(gate) !== GATE_ROOT
    || !supportedGateName(gateName)
    || (publicGate && Object.prototype.hasOwnProperty.call(env, "PLATFORM_LIVE_USER_QA_SESSIONS"))
    || (liveUserGate && sessions !== path.join(gate, "browser-sessions.json"))
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
    || !supportedGateName(path.basename(binding.gate))
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
    const publicGate = binding !== null && PUBLIC_GATE_NAME.test(path.basename(binding.gate));
    const summary = summarizeRun(this.suite, result?.status, { publicGate });
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
