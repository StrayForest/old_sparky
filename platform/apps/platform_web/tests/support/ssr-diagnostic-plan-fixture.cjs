"use strict";

// Hermetic Next-server fixture. It maps only the production plan and release
// identity paths to private temp files, while leaving the actual plan parser,
// /proc identity checks, proxy, RootLayout and response guard untouched.
const fs = require("node:fs");
const path = require("node:path");
const { tmpdir } = require("node:os");
const { syncBuiltinESMExports } = require("node:module");
const { randomUUID } = require("node:crypto");

const FIXTURE_FLAG = "PLATFORM_SSR_DIAGNOSTIC_E2E_FIXTURE";
const PLAN_DIR = "/run/oldsparky-platform";
const PLAN_PATH = `${PLAN_DIR}/performance-diagnostic-plan.web.json`;
const runId = process.env.PLATFORM_SSR_DIAGNOSTIC_RUN_ID ?? "";
const requestId = process.env.PLATFORM_SSR_DIAGNOSTIC_REQUEST_ID ?? "";
const sourceSha = process.env.PLATFORM_SSR_DIAGNOSTIC_SOURCE_SHA ?? "";
const invocationId = process.env.INVOCATION_ID ?? "";
const fixtureRoot = process.env.PLATFORM_SSR_DIAGNOSTIC_FIXTURE_ROOT ?? "";
const normalizedFixtureRoot = path.resolve(fixtureRoot || ".");
const standaloneServer = process.argv.some((argument) =>
  path.normalize(argument).endsWith(path.join(".next", "standalone", "server.js")),
);

if (process.env[FIXTURE_FLAG] !== "1" || !standaloneServer
    || path.dirname(normalizedFixtureRoot) !== tmpdir()
    || !/^platform-ssr-diagnostic-e2e-\d+-[0-9a-f-]{36}$/u.test(path.basename(normalizedFixtureRoot))
    || !/^[0-9a-f]{32}$/u.test(runId)
    || !/^[0-9a-f]{32}$/u.test(requestId)
    || !/^[0-9a-f]{40}$/u.test(sourceSha)
    || !/^[0-9a-f-]{36}$/u.test(invocationId)
    || process.env.PLATFORM_RUNTIME_SERVICE !== "web"
    || process.env.HOSTNAME !== "127.0.0.1"
    || process.env.PORT !== "3100"
    || process.env.PLATFORM_API_BASE_URL !== "http://127.0.0.1:3199/api/v1") {
  throw new Error("SSR diagnostic E2E fixture refused an unexpected process context");
}

const fixtureStat = fs.statSync;
const fixtureLstat = fs.lstatSync;
const fixtureOpen = fs.openSync;
const fixtureFstat = fs.fstatSync;
const fixtureClose = fs.closeSync;
const openedFixtures = new Map();
const fixtureReleaseSlug = "ssr-diagnostic-e2e";
const nowMs = Date.now();
const plan = {
  schema: 1,
  run_id: runId,
  source_sha: sourceSha,
  release_slug: fixtureReleaseSlug,
  workload: "authenticated_html_ssr_capacity_120s_v1",
  off_start_ms: nowMs + 12_000,
  off_end_ms: nowMs + 32_000,
  on_start_ms: nowMs + 37_000,
  on_end_ms: nowMs + 157_000,
  expires_at_ms: nowMs + 172_000,
  targets: [],
};

function currentStartTicks() {
  const raw = fs.readFileSync("/proc/self/stat", "ascii");
  const closingParen = raw.lastIndexOf(")");
  if (closingParen < 0) throw new Error("unable to parse current process stat");
  const fields = raw.slice(closingParen + 2).trim().split(/\s+/u);
  const ticks = Number(fields[19]);
  if (!Number.isSafeInteger(ticks) || ticks <= 0) {
    throw new Error("current process start ticks are invalid");
  }
  return ticks;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

fs.mkdirSync(fixtureRoot, { mode: 0o700 });
let fixtureCleaned = false;
function cleanupFixture() {
  if (fixtureCleaned) return;
  fixtureCleaned = true;
  try {
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  } catch {
    // Keep test teardown best-effort; the hermetic outer runner owns its temp
    // build and does not touch production paths.
  }
}
process.once("exit", cleanupFixture);
process.once("SIGINT", () => {
  cleanupFixture();
  process.exit(130);
});
process.once("SIGTERM", () => {
  cleanupFixture();
  process.exit(143);
});

const planFile = path.join(fixtureRoot, "plan.json");
const releaseFile = path.join(fixtureRoot, "RELEASE.json");
const fixtureReport = path.join(fixtureRoot, "fixture.json");
plan.targets = [{
  service: "web",
  pid: process.pid,
  start_ticks: currentStartTicks(),
  invocation_id: invocationId,
}];
fs.writeFileSync(planFile, `${canonicalJson(plan)}\n`, { mode: 0o600, flag: "wx" });
fs.writeFileSync(releaseFile, `${canonicalJson({
  release_slug: fixtureReleaseSlug,
  source_git_commit: sourceSha,
})}\n`, { mode: 0o600, flag: "wx" });
fs.writeFileSync(fixtureReport, `${canonicalJson({
  run_id: runId,
  source_sha: sourceSha,
  release_slug: fixtureReleaseSlug,
  request_path: "/tournaments/night-veil-open-5",
  off_start_ms: plan.off_start_ms,
  off_end_ms: plan.off_end_ms,
  selector_matched_calls: 0,
  selector_accepted: false,
})}\n`, { mode: 0o600, flag: "wx" });

const selectorSymbol = Symbol.for("old-sparky.ssr-stream-selector");
const actualSelector = globalThis[selectorSymbol];
if (typeof actualSelector !== "function") {
  throw new Error("SSR diagnostic E2E fixture found no installed stream selector");
}
let selectorMatchedCalls = 0;
let selectorAccepted = false;
globalThis[selectorSymbol] = function observeSsrDiagnosticSelection(entry) {
  const result = Reflect.apply(actualSelector, this, [entry]);
  if (entry?.runId === runId && entry?.requestId === requestId) {
    selectorMatchedCalls = Math.min(4, selectorMatchedCalls + 1);
    selectorAccepted = selectorAccepted || result === true;
    try {
      fs.writeFileSync(fixtureReport, `${canonicalJson({
        run_id: runId,
        source_sha: sourceSha,
        release_slug: fixtureReleaseSlug,
        request_path: "/tournaments/night-veil-open-5",
        off_start_ms: plan.off_start_ms,
        off_end_ms: plan.off_end_ms,
        selector_matched_calls: selectorMatchedCalls,
        selector_accepted: selectorAccepted,
      })}\n`, { mode: 0o600 });
    } catch {
      // Instrumentation is test-only and must never alter SSR behavior.
    }
  }
  return result;
};

function realPathFor(virtualPath) {
  const normalized = typeof virtualPath === "string" ? path.resolve(virtualPath) : "";
  if (normalized === path.resolve(PLAN_PATH)) return planFile;
  const expectedReleasePath = path.resolve(process.cwd(), "../../../..", "RELEASE.json");
  if (normalized === expectedReleasePath) return releaseFile;
  return null;
}

function rootedStats(stats, { directory = false } = {}) {
  return new Proxy(stats, {
    get(target, property) {
      if (directory) {
        if (property === "uid" || property === "gid") return 0;
        if (property === "mode") return (target.mode & ~0o777) | 0o711;
        if (property === "isDirectory") return () => true;
        if (property === "isFile") return () => false;
        if (property === "isSymbolicLink") return () => false;
      } else {
        if (property === "uid") return 0;
        if (property === "gid") return process.getgid();
        if (property === "nlink") return 1;
        if (property === "mode") return (target.mode & ~0o777) | 0o440;
        if (property === "isSymbolicLink") return () => false;
      }
      return Reflect.get(target, property, target);
    },
  });
}

fs.lstatSync = function ssrDiagnosticFixtureLstat(candidate, ...args) {
  const normalized = typeof candidate === "string" ? path.resolve(candidate) : "";
  if (normalized === path.resolve(PLAN_DIR)) {
    return rootedStats(fixtureStat(fixtureRoot), { directory: true });
  }
  const mapped = realPathFor(candidate);
  if (mapped !== null) return rootedStats(fixtureLstat(mapped));
  return fixtureLstat.call(fs, candidate, ...args);
};

fs.openSync = function ssrDiagnosticFixtureOpen(candidate, flags, ...args) {
  const mapped = realPathFor(candidate);
  if (mapped === null) return fixtureOpen.call(fs, candidate, flags, ...args);
  const descriptor = fixtureOpen.call(fs, mapped, flags, ...args);
  openedFixtures.set(descriptor, mapped);
  return descriptor;
};

fs.fstatSync = function ssrDiagnosticFixtureFstat(descriptor, ...args) {
  const mapped = openedFixtures.get(descriptor);
  const stats = fixtureFstat.call(fs, descriptor, ...args);
  return mapped === undefined ? stats : rootedStats(stats);
};

fs.closeSync = function ssrDiagnosticFixtureClose(descriptor, ...args) {
  openedFixtures.delete(descriptor);
  return fixtureClose.call(fs, descriptor, ...args);
};

syncBuiltinESMExports();
