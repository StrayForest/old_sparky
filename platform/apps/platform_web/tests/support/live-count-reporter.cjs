"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This helper is CommonJS. */

const fs = require("node:fs");
const path = require("node:path");

const MAX_TESTS = 4096;
const MAX_REPORT_BYTES = 1024;
const MAX_FAILURE_REPORT_BYTES = 16 * 1024;
const MAX_FAILURE_IDENTITY_REPORT_BYTES = 16 * 1024;
const MAX_FAILURE_IDENTITY_RECORDS = 128;
// The guard adds three fixed metadata fields (two 64-char digests and a
// 20-char UTC timestamp) before durable publication. Keep a little headroom
// beyond their measured 219-byte JSON envelope while retaining the 16 KiB
// durable-file limit.
const FAILURE_REPORT_GUARD_ENVELOPE_RESERVE_BYTES = 256;
const MAX_EPHEMERAL_FAILURE_REPORT_BYTES = (
  MAX_FAILURE_REPORT_BYTES - FAILURE_REPORT_GUARD_ENVELOPE_RESERVE_BYTES
);
const MAX_PAGE_ERROR_EVIDENCE_BYTES = 2048;
const MAX_PAGE_ERROR_EVENTS = 4;
const MAX_PAGE_ERROR_STACK_CHARS = 16 * 1024;
const MAX_PAGE_ERROR_STACK_LINES = 32;
const MAX_TRACKED_NEXT_SCRIPTS = 256;
const MAX_IMAGE_INVENTORY_COUNT = 512;
const MAX_IMAGE_INVENTORY_ATTACHMENT_BYTES = 8 * 1024;
const MAX_IMAGE_DIAGNOSTIC_ENTRIES = 16;
const MAX_IMAGE_DIAGNOSTIC_EVENTS = 64;
const MAX_ATTEMPTS = 32768;
const GATE_ROOT = "/run/oldsparky-liveqa";
const FAILURE_REPORT_FILE = "live-failures-v2.json";
const FAILURE_IDENTITY_REPORT_FILE = "live-failure-identities-v1.json";
const LIVE_USER_FAILURE_REPORT_FILE = "live-user-failures-v3.json";
const PAGE_ERROR_ATTACHMENT_NAME = "live-pageerror-evidence-v1";
const IMAGE_INVENTORY_ATTACHMENT_NAMES = new Set([
  "live-image-inventory-evidence-v1",
  "live-image-inventory-evidence-v2",
]);
const LIVE_USER_API_FAILURE_ATTACHMENT_NAME = "live-user-api-failure-v1";
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
const PUBLIC_LOGICAL_TOTAL = 57;
const EXPECTED_PUBLIC_SKIP_TYPE = "public-live-qa-expected-skip";
const PUBLIC_PROJECTS = new Set([
  "live-desktop",
  "live-mobile",
  "live-webkit-mobile",
]);
const PUBLIC_TEST_IDS = new Map([
  ["live route / renders without horizontal overflow", "route_root_overflow"],
  ["live route /info renders without horizontal overflow", "route_info_overflow"],
  ["live route /tournaments renders without horizontal overflow", "route_tournaments_overflow"],
  ["live route /tournaments/new renders without horizontal overflow", "route_tournaments_new_overflow"],
  ["live route /profile/me renders without horizontal overflow", "route_profile_me_overflow"],
  ["live route /profile/lisalexy renders without horizontal overflow", "route_profile_lisalexy_overflow"],
  ["production Chromium live QA keeps its process sandbox enabled", "chromium_sandbox"],
  ["live CSP nonce is stable on soft navigation and rotates on hard reload", "csp_nonce_rotation"],
  ["local enforced CSP blocks negative inline and external probes", "csp_negative_probes"],
  ["live tournaments hub exposes a valid empty or populated list", "tournament_hub"],
  ["live 1920 catalog stays contained after every card asset loads", "tournament_catalog_containment"],
  ["live tournament detail and bracket routes render from the current public data", "tournament_detail_bracket"],
  ["live admin route is protected for anonymous users", "admin_anonymous_protection"],
  ["live home uses text-only tournament steps without overflow", "home_steps_overflow"],
  ["live public contact surfaces do not publish the support recipient", "contact_recipient_privacy"],
  ["live current patch route and objective source icons remain available", "objective_icons"],
  ["live four current patch DTOs render owner-scoped hero and ability changes", "patch_owner_sections"],
  ["live Cloudflare Analytics is conditionally observed without widening CSP", "analytics_csp"],
  ["live image currentSrc inventory stays inside the exact CSP hosts", "image_csp_hosts"],
]);
const FAILURE_SOURCE_FILES = new Set([
  "tests/smoke/live-launch.spec.ts",
  "tests/support/live-qa-sandbox.ts",
]);
const LIVE_USER_FAILURE_SOURCE_FILES = new Set([
  "tests/smoke/live-user-journey.spec.ts",
]);
const LIVE_USER_TEST_TITLE = "live player completes the accelerated tournament journey through visible controls";
const LIVE_USER_TEST_ID = "live_user_journey";
const LIVE_USER_PATH_SEGMENT = "[A-Za-z0-9_-]+";
const LIVE_USER_API_OPERATIONS = Object.freeze({
  tournament_create: Object.freeze({
    method: "POST", expected_status: 201, path_pattern: /^\/tournaments$/u,
  }),
  tournament_status: Object.freeze({
    method: "PATCH", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/status$`, "u"),
  }),
  invite_create: Object.freeze({
    method: "POST", expected_status: 201,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/invites$`, "u"),
  }),
  invite_claim: Object.freeze({
    method: "POST", expected_status: 201, path_pattern: /^\/tournaments\/invites\/claim$/u,
  }),
  tournament_join: Object.freeze({
    method: "POST", expected_status: 201,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/join$`, "u"),
  }),
  ready_check_start: Object.freeze({
    method: "POST", expected_status: 201,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/ready-check/start$`, "u"),
  }),
  ready_check_vote: Object.freeze({
    method: "POST", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/ready-check/vote$`, "u"),
  }),
  ready_check_close: Object.freeze({
    method: "POST", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/ready-check/close$`, "u"),
  }),
  captain_round_start: Object.freeze({
    method: "POST", expected_status: 201,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/captain-round/start$`, "u"),
  }),
  assignment_start: Object.freeze({
    method: "POST", expected_status: 202,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/auto-assignment/run-async$`, "u"),
  }),
  assignment_state: Object.freeze({
    method: "GET", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/auto-assignment$`, "u"),
  }),
  assignment_publish: Object.freeze({
    method: "POST", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/auto-assignment/${LIVE_USER_PATH_SEGMENT}/publish$`, "u"),
  }),
  assignment_lock: Object.freeze({
    method: "POST", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/deadlock/auto-assignment/${LIVE_USER_PATH_SEGMENT}/lock$`, "u"),
  }),
  bracket_read: Object.freeze({
    method: "GET", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/bracket\\?teams_view=summary$`, "u"),
  }),
  match_schedule: Object.freeze({
    method: "PATCH", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}/matches/${LIVE_USER_PATH_SEGMENT}/schedule$`, "u"),
  }),
  tournament_read: Object.freeze({
    method: "GET", expected_status: 200,
    path_pattern: new RegExp(`^/tournaments/${LIVE_USER_PATH_SEGMENT}$`, "u"),
  }),
});
const LIVE_USER_API_FAILURE_KINDS = new Set([
  "unexpected_status",
  "invalid_json",
  "request_error",
]);
const LIVE_USER_API_FAILURE_MESSAGES = Object.freeze({
  unexpected_status: "LiveUserApiUnexpectedStatus: API response status did not match.",
  invalid_json: "LiveUserApiInvalidJson: API response body was invalid.",
  request_error: "LiveUserApiRequestError: API request failed.",
});
const LIVE_USER_API_FAILURE_MAX_BYTES = 1024;
const FAILURE_ATTEMPT_STATUSES = new Set(["failed", "timedOut", "interrupted"]);
const PAGE_ERROR_CLASSES = new Set([
  "Error",
  "TypeError",
  "ReferenceError",
  "SyntaxError",
  "RangeError",
  "URIError",
  "EvalError",
  "AggregateError",
  "DOMException",
  "unknown",
]);
const PAGE_ERROR_ROUTES = new Set(["home", "patch", "tournaments", "other", "unknown"]);
const PAGE_ERROR_ASSET_BASENAME = /^(?:[a-z0-9][a-z0-9_-]{12}|turbopack-[a-z0-9_-]{13})\.js$/u;
const PAGE_ERROR_SCRIPT_SEGMENT = /^[A-Za-z0-9()[\]@._~-]+$/u;
const PAGE_ERROR_LIVE_ORIGIN = "https://old-sparky.com";
const PAGE_ERROR_ATTACHMENT_SCHEMA = 1;
const PAGE_ERROR_JS_CLASSES = new Set([
  "Error",
  "TypeError",
  "ReferenceError",
  "SyntaxError",
  "RangeError",
  "URIError",
  "EvalError",
  "AggregateError",
]);
const PAGE_ERROR_DOM_EXCEPTION_NAMES = new Set([
  "AbortError",
  "DataError",
  "EncodingError",
  "InvalidAccessError",
  "InvalidStateError",
  "NetworkError",
  "NotAllowedError",
  "NotFoundError",
  "NotReadableError",
  "NotSupportedError",
  "OperationError",
  "QuotaExceededError",
  "SecurityError",
  "TimeoutError",
  "TransactionInactiveError",
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
  "live current patch route and objective source icons remain available",
  "live four current patch DTOs render owner-scoped hero and ability changes",
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

function publicFailureIdentityMappingIsComplete() {
  if (PUBLIC_TEST_IDS.size !== PUBLIC_TEST_TITLES.size) return false;
  const ids = [...PUBLIC_TEST_IDS.values()];
  if (new Set(ids).size !== ids.length) return false;
  return [...PUBLIC_TEST_TITLES].every((title) => PUBLIC_TEST_IDS.has(title));
}

function publicTestTitles() {
  return [...PUBLIC_TEST_TITLES];
}

function publicCoverageIsComplete(tests, logical) {
  if (
    tests.length !== PUBLIC_LOGICAL_TOTAL
    || logical.skip < 6
    || logical.skip > 10
    || !publicFailureIdentityMappingIsComplete()
  ) return false;
  const seen = new Set();
  for (const test of tests) {
    const project = typeof test.parent?.project === "function"
      ? test.parent.project()?.name
      : undefined;
    if (
      typeof test.title !== "string"
      || !PUBLIC_TEST_TITLES.has(test.title)
      || !PUBLIC_TEST_IDS.has(test.title)
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

function bindingsFromEnvironment(env = process.env, { gateRoot = GATE_ROOT } = {}) {
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
    || path.dirname(gate) !== gateRoot
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

function normalizedFailureLocation(
  location,
  sourceRoot,
  fallbackKind,
  sourceFiles = FAILURE_SOURCE_FILES,
) {
  if (
    !location
    || typeof location.file !== "string"
    || location.file.length > 4096
    || /[\\\u0000-\u001f\u007f]/u.test(location.file)
    || location.file.split(/[\\/]/u).includes("..")
    || !Number.isSafeInteger(location.line)
    || location.line < 1
    || location.line > 10000
    || !Number.isSafeInteger(location.column)
    || location.column < 1
    || location.column > 10000
  ) return null;

  let root;
  let candidate;
  let rootMetadata;
  let fileMetadata;
  try {
    root = fs.realpathSync(sourceRoot);
    candidate = path.resolve(root, location.file);
    const relative = path.relative(root, candidate).split(path.sep).join("/");
    if (
      !relative
      || relative.startsWith("../")
      || relative === ".."
      || relative !== path.posix.normalize(relative)
      || !sourceFiles.has(relative)
    ) return null;
    rootMetadata = fs.lstatSync(root);
    fileMetadata = fs.lstatSync(candidate);
    if (
      !rootMetadata.isDirectory()
      || rootMetadata.isSymbolicLink()
      || !fileMetadata.isFile()
      || fileMetadata.isSymbolicLink()
      || fs.realpathSync(candidate) !== candidate
    ) return null;
    const sourceLines = fs.readFileSync(candidate, "utf8").split("\n");
    if (
      location.line > sourceLines.length
      || location.column > sourceLines[location.line - 1].length + 1
    ) return null;
    const sourceLine = sourceLines[location.line - 1];
    const assertion = /\bexpect(?:\.poll)?\s*\(|\.to[A-Z][A-Za-z0-9]*\s*\(/u.test(sourceLine);
    return {
      file: relative,
      line: location.line,
      column: location.column,
      kind: fallbackKind === "declaration"
        ? "declaration"
        : assertion ? "assertion" : "test_source",
    };
  } catch {
    return null;
  }
}

function unavailablePageErrorEvidence() {
  return { state: "unavailable", truncated: false, events: [] };
}

function pageErrorClass(error) {
  let name = "";
  try {
    if (typeof error?.name === "string") name = error.name;
  } catch {
    return "unknown";
  }
  if (PAGE_ERROR_JS_CLASSES.has(name)) return name;
  if (PAGE_ERROR_DOM_EXCEPTION_NAMES.has(name)) return "DOMException";
  return "unknown";
}

function pageErrorRoute(pageUrl) {
  if (typeof pageUrl !== "string" || pageUrl.length > 2048) return "unknown";
  try {
    const parsed = new URL(pageUrl);
    if (
      parsed.origin !== PAGE_ERROR_LIVE_ORIGIN
      || parsed.username
      || parsed.password
    ) return "unknown";
    if (parsed.pathname === "/") return "home";
    if (/^\/patches\/[0-9]{1,32}$/u.test(parsed.pathname)) return "patch";
    if (parsed.pathname === "/tournaments") return "tournaments";
    return "other";
  } catch {
    return "unknown";
  }
}

function trustedNextScriptPath(rawUrl) {
  if (
    typeof rawUrl !== "string"
    || rawUrl.length > 2048
    || !rawUrl.startsWith(`${PAGE_ERROR_LIVE_ORIGIN}/_next/static/chunks/`)
    || rawUrl.includes("?")
    || rawUrl.includes("#")
    || rawUrl.includes("%")
    || rawUrl.includes("\\")
  ) return null;
  try {
    const parsed = new URL(rawUrl);
    if (
      parsed.origin !== PAGE_ERROR_LIVE_ORIGIN
      || parsed.username
      || parsed.password
      || parsed.search
      || parsed.hash
      || `${parsed.origin}${parsed.pathname}` !== rawUrl
    ) return null;
    const relative = parsed.pathname.slice("/_next/static/chunks/".length);
    const segments = relative.split("/");
    const basename = segments.at(-1) ?? "";
    if (
      segments.some((segment) => (
        !segment
        || segment === "."
        || segment === ".."
        || !PAGE_ERROR_SCRIPT_SEGMENT.test(segment)
      ))
      || !PAGE_ERROR_ASSET_BASENAME.test(basename)
    ) return null;
    return { path: parsed.pathname, basename };
  } catch {
    return null;
  }
}

function trustedSuccessfulNextScriptPath(response) {
  if (
    !response
    || response.resourceType !== "script"
    || !Number.isSafeInteger(response.status)
    || response.status < 200
    || response.status >= 300
  ) return null;
  return trustedNextScriptPath(response.url)?.path ?? null;
}

function pageErrorLocation(stack, successfulScriptPaths) {
  if (
    typeof stack !== "string"
    || stack.length > MAX_PAGE_ERROR_STACK_CHARS
    || !(successfulScriptPaths instanceof Set)
  ) return null;
  for (const stackLine of stack.split(/\r?\n/u).slice(0, MAX_PAGE_ERROR_STACK_LINES)) {
    const match = stackLine.match(
      /(?:^|\s|\()(https:\/\/old-sparky\.com\/\S+\.js):([1-9][0-9]{0,6}):([1-9][0-9]{0,6})\)?$/u,
    );
    if (!match) continue;
    const script = trustedNextScriptPath(match[1]);
    const line = Number(match[2]);
    const column = Number(match[3]);
    if (
      !script
      || !successfulScriptPaths.has(script.path)
      || !Number.isSafeInteger(line)
      || line > 1_000_000
      || !Number.isSafeInteger(column)
      || column > 1_000_000
    ) continue;
    return { asset_basename: script.basename, line, column };
  }
  return null;
}

function projectPageErrorEvent(error, pageUrl, successfulScriptPaths) {
  let stack;
  try {
    stack = error?.stack;
  } catch {
    stack = null;
  }
  const location = pageErrorLocation(stack, successfulScriptPaths);
  return {
    class: pageErrorClass(error),
    route: pageErrorRoute(pageUrl),
    asset_basename: location?.asset_basename ?? null,
    line: location?.line ?? null,
    column: location?.column ?? null,
  };
}

function createPageErrorAttachment(input) {
  if (
    !input
    || typeof input !== "object"
    || !Array.isArray(input.errors)
    || !Array.isArray(input.successfulScriptResponses)
  ) return null;
  const successfulScriptPaths = new Set();
  for (const response of input.successfulScriptResponses.slice(0, MAX_TRACKED_NEXT_SCRIPTS)) {
    const scriptPath = trustedSuccessfulNextScriptPath(response);
    if (scriptPath) successfulScriptPaths.add(scriptPath);
  }
  const events = input.errors
    .slice(0, MAX_PAGE_ERROR_EVENTS)
    .map((error) => projectPageErrorEvent(error, input.pageUrl, successfulScriptPaths));
  const payload = {
    schema: PAGE_ERROR_ATTACHMENT_SCHEMA,
    truncated: input.truncated === true || input.errors.length > events.length,
    events,
  };
  const body = Buffer.from(JSON.stringify(payload), "ascii");
  if (body.length > MAX_PAGE_ERROR_EVIDENCE_BYTES) return null;
  return {
    name: PAGE_ERROR_ATTACHMENT_NAME,
    contentType: "application/json",
    body,
  };
}

function normalizedPageErrorEvent(event) {
  if (
    !event
    || typeof event !== "object"
    || Array.isArray(event)
    || Object.keys(event).sort().join(",") !== "asset_basename,class,column,line,route"
    || typeof event.class !== "string"
    || !PAGE_ERROR_CLASSES.has(event.class)
    || typeof event.route !== "string"
    || !PAGE_ERROR_ROUTES.has(event.route)
  ) return null;
  const noAsset = event.asset_basename === null
    && event.line === null
    && event.column === null;
  const withAsset = typeof event.asset_basename === "string"
    && PAGE_ERROR_ASSET_BASENAME.test(event.asset_basename)
    && Number.isSafeInteger(event.line)
    && event.line >= 1
    && event.line <= 1_000_000
    && Number.isSafeInteger(event.column)
    && event.column >= 1
    && event.column <= 1_000_000;
  if (!noAsset && !withAsset) return null;
  return [
    event.class,
    event.route,
    noAsset ? null : event.asset_basename,
    noAsset ? null : event.line,
    noAsset ? null : event.column,
  ];
}

function pageErrorEvidenceForAttempt(result) {
  if (!Array.isArray(result?.attachments)) return unavailablePageErrorEvidence();
  const attachments = result.attachments.filter(
    (attachment) => attachment?.name === PAGE_ERROR_ATTACHMENT_NAME,
  );
  if (attachments.length !== 1) return unavailablePageErrorEvidence();
  const attachment = attachments[0];
  if (
    attachment?.contentType !== "application/json"
    || attachment.path !== undefined
    || !Buffer.isBuffer(attachment.body)
    || attachment.body.length > MAX_PAGE_ERROR_EVIDENCE_BYTES
    || attachment.body.some((byte) => byte > 0x7f)
  ) return unavailablePageErrorEvidence();

  const raw = attachment.body;
  let payload;
  try {
    payload = JSON.parse(attachment.body.toString("ascii"));
  } catch {
    return unavailablePageErrorEvidence();
  }
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || Object.keys(payload).sort().join(",") !== "events,schema,truncated"
    || payload.schema !== 1
    || typeof payload.truncated !== "boolean"
    || !Array.isArray(payload.events)
    || payload.events.length > MAX_PAGE_ERROR_EVENTS
    || (payload.truncated && payload.events.length !== MAX_PAGE_ERROR_EVENTS)
    || raw.toString("ascii") !== JSON.stringify(payload)
  ) return unavailablePageErrorEvidence();
  const events = payload.events.map(normalizedPageErrorEvent);
  if (events.some((event) => event === null)) return unavailablePageErrorEvidence();
  if (events.length === 0 && payload.truncated) return unavailablePageErrorEvidence();
  return {
    state: events.length === 0 ? "absent" : "captured",
    truncated: payload.truncated,
    events,
  };
}

function normalizedImageInventoryAttachment(attachment) {
  if (
    !IMAGE_INVENTORY_ATTACHMENT_NAMES.has(attachment?.name)
    || attachment.contentType !== "application/json"
    || attachment.path !== undefined
    || !Buffer.isBuffer(attachment.body)
    || attachment.body.length > MAX_IMAGE_INVENTORY_ATTACHMENT_BYTES
    || attachment.body.some((byte) => byte > 0x7f)
  ) return null;
  let payload;
  try {
    payload = JSON.parse(attachment.body.toString("ascii"));
  } catch {
    return null;
  }
  const versionOneKeys = [
    "complete",
    "counts_truncated",
    "image_count",
    "missing_current_src_count",
    "route",
    "schema",
    "unloaded_count",
  ];
  const versionTwoKeys = [
    "complete",
    "counts_truncated",
    "coverage",
    "image_count",
    "image_entries_truncated",
    "images",
    "missing_current_src_count",
    "route",
    "schema",
    "unloaded_count",
  ];
  const schema = payload?.schema;
  const expectedKeys = schema === 1 ? versionOneKeys : schema === 2 ? versionTwoKeys : [];
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || expectedKeys.length === 0
    || Object.keys(payload).sort().join(",") !== expectedKeys.join(",")
    || !["home", "patch", "tournaments"].includes(payload.route)
    || !Number.isSafeInteger(payload.image_count)
    || payload.image_count < 0
    || payload.image_count > MAX_IMAGE_INVENTORY_COUNT
    || !Number.isSafeInteger(payload.missing_current_src_count)
    || payload.missing_current_src_count < 0
    || payload.missing_current_src_count > MAX_IMAGE_INVENTORY_COUNT
    || !Number.isSafeInteger(payload.unloaded_count)
    || payload.unloaded_count < 0
    || payload.unloaded_count > MAX_IMAGE_INVENTORY_COUNT
    || typeof payload.complete !== "boolean"
    || typeof payload.counts_truncated !== "boolean"
    || payload.missing_current_src_count > payload.image_count
    || payload.unloaded_count > payload.image_count
    || (payload.counts_truncated && payload.image_count !== MAX_IMAGE_INVENTORY_COUNT)
    || (payload.complete && (
      payload.image_count === 0
      || payload.missing_current_src_count !== 0
      || payload.unloaded_count !== 0
    ))
    || (schema === 2 && !normalizedImageInventoryV2(payload))
    || attachment.body.toString("ascii") !== JSON.stringify(payload)
  ) return null;
  return payload;
}

function normalizedImageInventoryV2(payload) {
  const coverage = payload.coverage;
  const images = payload.images;
  if (
    !coverage
    || typeof coverage !== "object"
    || Array.isArray(coverage)
    || Object.keys(coverage).sort().join(",") !== [
      "csp_events_truncated",
      "csp_state",
      "network_state",
      "unmatched_csp_count",
    ].join(",")
    || !["captured", "truncated", "unavailable"].includes(coverage.network_state)
    || !["captured", "truncated", "unavailable"].includes(coverage.csp_state)
    || !Number.isSafeInteger(coverage.unmatched_csp_count)
    || coverage.unmatched_csp_count < 0
    || coverage.unmatched_csp_count > MAX_IMAGE_DIAGNOSTIC_EVENTS
    || typeof coverage.csp_events_truncated !== "boolean"
    || (coverage.csp_events_truncated && coverage.csp_state !== "truncated")
    || !Array.isArray(images)
    || images.length > MAX_IMAGE_DIAGNOSTIC_ENTRIES
    || typeof payload.image_entries_truncated !== "boolean"
    || (payload.image_entries_truncated && images.length >= payload.image_count)
    || (!payload.image_entries_truncated && images.length !== payload.image_count)
  ) return false;

  const seenOrdinals = new Set();
  for (const image of images) {
    const expectedImageKeys = [
      "complete",
      "csp_image_block_count",
      "csp_state",
      "current_src_present",
      "generation",
      "natural_width_positive",
      "loading",
      "ordinal",
      "request_failure_class",
      "request_failed",
      "request_finished",
      "request_state",
      "response_received",
      "response_status_class",
      "scroll_attempted",
      "src_present",
      "srcset_present",
      "terminal_duration_bucket",
      "viewport_state",
    ];
    if (
      !image
      || typeof image !== "object"
      || Array.isArray(image)
      || Object.keys(image).sort().join(",") !== expectedImageKeys.sort().join(",")
      || !Number.isSafeInteger(image.ordinal)
      || image.ordinal < 1
      || image.ordinal > MAX_IMAGE_INVENTORY_COUNT
      || seenOrdinals.has(image.ordinal)
      || !Number.isSafeInteger(image.generation)
      || image.generation < 1
      || image.generation > 1024
      || [
        image.src_present,
        image.srcset_present,
        image.complete,
        image.natural_width_positive,
        image.current_src_present,
        image.scroll_attempted,
      ].some((value) => typeof value !== "boolean")
      || !["lazy", "eager", "unknown"].includes(image.loading)
      || !["intersecting", "outside", "unknown"].includes(image.viewport_state)
      || ![
        "not_observed", "pending", "response_received", "request_finished",
        "request_failed", "ambiguous", "unavailable",
      ].includes(image.request_state)
      || ![true, false, null].includes(image.response_received)
      || ![
        "none", "2xx", "3xx", "4xx", "5xx", "other", "ambiguous", "unavailable",
      ].includes(image.response_status_class)
      || ![true, false, null].includes(image.request_finished)
      || ![true, false, null].includes(image.request_failed)
      || ![
        "none", "dns", "connection", "timeout", "tls", "aborted", "other",
        "ambiguous", "unavailable",
      ].includes(image.request_failure_class)
      || !["matched", "not_observed", "ambiguous", "unavailable"].includes(image.csp_state)
      || !Number.isSafeInteger(image.csp_image_block_count)
      || image.csp_image_block_count < 0
      || image.csp_image_block_count > MAX_IMAGE_DIAGNOSTIC_EVENTS
      || ![
        "<100ms", "<1s", "<5s", ">=5s", "not_observed", "ambiguous", "unavailable",
      ].includes(image.terminal_duration_bucket)
    ) return false;

    if (
      (image.request_state === "not_observed" && (
        image.response_received !== false
        || image.response_status_class !== "none"
        || image.request_finished !== false
        || image.request_failed !== false
        || image.request_failure_class !== "none"
        || image.terminal_duration_bucket !== "not_observed"
      ))
      || (image.request_state === "pending" && (
        image.response_received !== false
        || image.response_status_class !== "none"
        || image.request_finished !== false
        || image.request_failed !== false
        || image.request_failure_class !== "none"
        || !["<100ms", "<1s", "<5s", ">=5s"].includes(image.terminal_duration_bucket)
      ))
      || (image.request_state === "response_received" && (
        image.response_received !== true
        || ["none", "ambiguous", "unavailable"].includes(image.response_status_class)
        || image.request_finished !== false
        || image.request_failed !== false
        || !["<100ms", "<1s", "<5s", ">=5s"].includes(image.terminal_duration_bucket)
      ))
      || (image.request_state === "request_finished" && (
        image.request_finished !== true
        || image.request_failed !== false
        || image.request_failure_class !== "none"
        || !["<100ms", "<1s", "<5s", ">=5s"].includes(image.terminal_duration_bucket)
      ))
      || (image.request_state === "request_failed" && (
        image.request_failed !== true
        || image.request_finished !== false
        || image.request_failure_class === "none"
        || !["<100ms", "<1s", "<5s", ">=5s"].includes(image.terminal_duration_bucket)
      ))
      || (["ambiguous", "unavailable"].includes(image.request_state) && (
        image.response_received !== null
        || image.response_status_class !== image.request_state
        || image.request_finished !== null
        || image.request_failed !== null
        || image.request_failure_class !== image.request_state
        || image.terminal_duration_bucket !== image.request_state
      ))
      || ((image.response_received === false) !== (image.response_status_class === "none"))
      || (image.response_received === true && ["none", "ambiguous", "unavailable"].includes(image.response_status_class))
      || (image.request_finished === true && image.request_failed === true)
      || (image.csp_state === "matched") !== (image.csp_image_block_count > 0)
      || (image.csp_state === "not_observed" && image.csp_image_block_count !== 0)
      || (coverage.network_state !== "captured" && image.request_state !== "unavailable")
      || (coverage.csp_state === "unavailable" && image.csp_state !== "unavailable")
      || (coverage.csp_state === "truncated" && image.csp_state === "not_observed")
    ) return false;
    seenOrdinals.add(image.ordinal);
  }
  return true;
}

function imageInventoryEvidenceForAttempt(result) {
  const attachments = Array.isArray(result?.attachments)
    ? result.attachments.filter(
      (attachment) => IMAGE_INVENTORY_ATTACHMENT_NAMES.has(attachment?.name),
    )
    : [];
  if (attachments.length === 0 || attachments.length > 3) {
    return { state: "unavailable", inventory: null };
  }
  const routes = [];
  const seenRoutes = new Set();
  let schema = null;
  for (const attachment of attachments) {
    const route = normalizedImageInventoryAttachment(attachment);
    if (!route || seenRoutes.has(route.route) || (schema !== null && schema !== route.schema)) {
      return { state: "unavailable", inventory: null };
    }
    schema = route.schema;
    seenRoutes.add(route.route);
    routes.push(route);
  }
  return { state: "captured", inventory: { schema, routes } };
}

function normalizedUserApiFailure(input) {
  const keys = input && typeof input === "object" && !Array.isArray(input)
    ? Object.keys(input).sort().join(",")
    : "";
  if (
    !input
    || typeof input !== "object"
    || Array.isArray(input)
    || ![
      "actual_status,expected_status,kind,method,operation,source_column,source_file,source_line",
      "actual_status,expected_status,kind,method,operation,schema,source_column,source_file,source_line",
    ].includes(keys)
    || (input.schema !== undefined && (typeOfSafeInteger(input.schema) !== 1))
    || !LIVE_USER_API_FAILURE_KINDS.has(input.kind)
    || typeof input.operation !== "string"
    || !Object.prototype.hasOwnProperty.call(LIVE_USER_API_OPERATIONS, input.operation)
  ) return null;
  const operation = LIVE_USER_API_OPERATIONS[input.operation];
  if (
    input.method !== operation.method
    || typeOfSafeInteger(input.expected_status) !== operation.expected_status
    || typeof input.source_file !== "string"
    || input.source_file !== "tests/smoke/live-user-journey.spec.ts"
    || !Number.isSafeInteger(input.source_line)
    || input.source_line < 1
    || input.source_line > 10000
    || !Number.isSafeInteger(input.source_column)
    || input.source_column < 1
    || input.source_column > 10000
  ) return null;
  if (input.kind === "request_error") {
    if (input.actual_status !== null) return null;
  } else if (
    !Number.isSafeInteger(input.actual_status)
    || input.actual_status < 100
    || input.actual_status > 599
    || (input.kind === "unexpected_status" && input.actual_status === operation.expected_status)
    || (input.kind === "invalid_json" && input.actual_status !== operation.expected_status)
  ) return null;
  return {
    kind: input.kind,
    operation: input.operation,
    method: operation.method,
    expected_status: operation.expected_status,
    actual_status: input.actual_status,
    source_file: input.source_file,
    source_line: input.source_line,
    source_column: input.source_column,
  };
}

function typeOfSafeInteger(value) {
  return Number.isSafeInteger(value) ? value : null;
}

function createLiveUserApiFailureAttachment(input) {
  const failure = normalizedUserApiFailure(input);
  if (!failure) return null;
  const payload = { schema: 1, ...failure };
  const body = Buffer.from(JSON.stringify(payload), "ascii");
  if (body.length > LIVE_USER_API_FAILURE_MAX_BYTES) return null;
  return {
    name: LIVE_USER_API_FAILURE_ATTACHMENT_NAME,
    contentType: "application/json",
    body,
  };
}

function userApiFailureMessage(kind) {
  return LIVE_USER_API_FAILURE_MESSAGES[kind] ?? null;
}

function matchesLiveUserApiOperation(operation, method, requestPath) {
  const contract = LIVE_USER_API_OPERATIONS[operation];
  return Boolean(
    contract
    && method === contract.method
    && typeof requestPath === "string"
    && requestPath.length <= 512
    && contract.path_pattern.test(requestPath)
  );
}

function liveUserApiFailureForAttempt(result, sourceRoot, failureLocation) {
  const unavailable = { state: "unavailable", failure: null };
  if (!Array.isArray(result?.attachments) || !failureLocation) return unavailable;
  const attachments = result.attachments.filter(
    (attachment) => attachment?.name === LIVE_USER_API_FAILURE_ATTACHMENT_NAME,
  );
  if (attachments.length !== 1) return unavailable;
  const attachment = attachments[0];
  if (
    attachment?.contentType !== "application/json"
    || attachment.path !== undefined
    || !Buffer.isBuffer(attachment.body)
    || attachment.body.length > LIVE_USER_API_FAILURE_MAX_BYTES
    || attachment.body.some((byte) => byte > 0x7f)
  ) return unavailable;
  let payload;
  try {
    payload = JSON.parse(attachment.body.toString("ascii"));
  } catch {
    return unavailable;
  }
  if (
    !payload
    || typeof payload !== "object"
    || Array.isArray(payload)
    || Object.keys(payload).sort().join(",")
      !== "actual_status,expected_status,kind,method,operation,schema,source_column,source_file,source_line"
    || payload.schema !== 1
    || attachment.body.toString("ascii") !== JSON.stringify(payload)
  ) return unavailable;
  const failure = normalizedUserApiFailure(payload);
  if (
    !failure
    || failure.source_file !== failureLocation.file
    || failure.source_line !== failureLocation.line
    || failure.source_column !== failureLocation.column
  ) return unavailable;
  const matchingErrors = Array.isArray(result.errors)
    ? result.errors.filter((error) => {
      const location = normalizedFailureLocation(
        error?.location,
        sourceRoot,
        "failure",
        LIVE_USER_FAILURE_SOURCE_FILES,
      );
      return location
        && location.file === failure.source_file
        && location.line === failure.source_line
        && location.column === failure.source_column;
    })
    : [];
  if (matchingErrors.length !== 1) return unavailable;
  return { state: "captured", failure: {
    kind: failure.kind,
    operation: failure.operation,
    method: failure.method,
    expected_status: failure.expected_status,
    actual_status: failure.actual_status,
  } };
}

function liveUserFailureRecordForAttempt(test, result, attempt, sourceRoot) {
  const project = typeof test?.parent?.project === "function"
    ? test.parent.project()?.name
    : undefined;
  if (
    test?.title !== LIVE_USER_TEST_TITLE
    || project !== "live-desktop"
    || !FAILURE_ATTEMPT_STATUSES.has(result?.status)
  ) return null;
  let location = null;
  if (Array.isArray(result.errors)) {
    for (const error of result.errors) {
      location = normalizedFailureLocation(
        error?.location,
        sourceRoot,
        "failure",
        LIVE_USER_FAILURE_SOURCE_FILES,
      );
      if (location) break;
    }
  }
  const sourceFrame = location
    ? { file: location.file, line: location.line, column: location.column }
    : null;
  const apiFailure = liveUserApiFailureForAttempt(result, sourceRoot, sourceFrame);
  return {
    id: LIVE_USER_TEST_ID,
    project,
    status: result.status,
    attempt,
    source_file: location?.file ?? null,
    source_line: location?.line ?? null,
    source_column: location?.column ?? null,
    source_kind: location ? "test_error_location" : "unavailable",
    api_failure_state: apiFailure.state,
    api_failure: apiFailure.failure,
  };
}

function writeBoundedLiveUserFailureSummary(
  binding,
  suite,
  summary,
  { gateRoot = GATE_ROOT, sourceRoot = process.cwd() } = {},
) {
  if (!binding || !summary || !suite || typeof suite.allTests !== "function") return false;
  const gateName = path.basename(binding.gate);
  if (
    path.dirname(binding.gate) !== gateRoot
    || !LIVE_USER_GATE_NAME.test(gateName)
    || !RUN_STATUSES.has(summary.run_status)
  ) return false;
  const tests = suite.allTests();
  if (!Array.isArray(tests) || tests.length !== 1) return false;
  const test = tests[0];
  if (
    !test
    || test.title !== LIVE_USER_TEST_TITLE
    || typeof test.outcome !== "function"
    || !Array.isArray(test.results)
  ) return false;
  const project = typeof test.parent?.project === "function"
    ? test.parent.project()?.name
    : undefined;
  if (project !== "live-desktop") return false;
  const failures = [];
  for (const result of test.results) {
    if (!FAILURE_ATTEMPT_STATUSES.has(result?.status)) continue;
    const failure = liveUserFailureRecordForAttempt(
      test,
      result,
      attemptsForTest(test, result),
      sourceRoot,
    );
    if (!failure) return false;
    failures.push(failure);
  }
  const failureExpected = summary.run_status !== "passed"
    || summary.logical?.fail > 0
    || summary.logical?.flaky > 0
    || summary.logical?.interrupted > 0
    || summary.attempts?.fail > 0
    || summary.attempts?.interrupted > 0
    || summary.attempts?.timedout > 0;
  if (!failureExpected || failures.length === 0) return false;

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
    || !gateMetadata.isDirectory()
    || gateMetadata.isSymbolicLink()
    || gateMetadata.uid !== process.geteuid()
    || (gateMetadata.mode & 0o777) !== 0o700
    || !resultsMetadata.isDirectory()
    || resultsMetadata.isSymbolicLink()
    || resultsMetadata.uid !== process.geteuid()
    || (resultsMetadata.mode & 0o777) !== 0o700
  ) return false;

  const payload = {
    schema: 3,
    gate: gateName,
    source_sha: binding.source_sha,
    app_sha: binding.app_sha,
    marker_sha256: binding.marker_sha256,
    run_status: summary.run_status,
    failures,
  };
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
  if (bytes.length > MAX_EPHEMERAL_FAILURE_REPORT_BYTES) return false;
  const output = path.join(binding.gate, "test-results", LIVE_USER_FAILURE_REPORT_FILE);
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
    ) return false;
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
    return true;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function failureRecordForAttempt(test, result, attempt, sourceRoot) {
  const project = typeof test?.parent?.project === "function"
    ? test.parent.project()?.name
    : undefined;
  const id = typeof test?.title === "string" ? PUBLIC_TEST_IDS.get(test.title) : undefined;
  if (!id || !PUBLIC_PROJECTS.has(project) || !FAILURE_ATTEMPT_STATUSES.has(result?.status)) {
    return null;
  }
  let location = null;
  if (Array.isArray(result.errors)) {
    for (const error of result.errors) {
      location = normalizedFailureLocation(error?.location, sourceRoot, "failure");
      if (location) break;
    }
  }
  if (!location) {
    location = normalizedFailureLocation(test.location, sourceRoot, "declaration");
  }
  const pageErrorEvidence = pageErrorEvidenceForAttempt(result);
  const failure = {
    id,
    project,
    status: result.status,
    attempt,
    source_file: location?.file ?? null,
    source_line: location?.line ?? null,
    source_column: location?.column ?? null,
    source_kind: location?.kind ?? "unavailable",
    page_error_state: pageErrorEvidence.state,
    page_errors_truncated: pageErrorEvidence.truncated,
    page_errors: pageErrorEvidence.events,
  };
  if (id === "image_csp_hosts") {
    const imageEvidence = imageInventoryEvidenceForAttempt(result);
    failure.image_inventory_state = imageEvidence.state;
    failure.image_inventory = imageEvidence.inventory;
  }
  return failure;
}

function boundedFailureReportBytes(payload) {
  let bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
  if (bytes.length <= MAX_EPHEMERAL_FAILURE_REPORT_BYTES) return bytes;

  // Keep one event per failed attempt before retaining additional detail.
  for (const failure of [...payload.failures].reverse()) {
    while (
      bytes.length > MAX_EPHEMERAL_FAILURE_REPORT_BYTES
      && failure.page_errors.length > 1
    ) {
      failure.page_errors.pop();
      failure.page_errors_truncated = true;
      bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
    }
  }
  // If bounded events are not enough, retain each failed test identity,
  // project, status and attempt while dropping later optional source locations
  // as an all-null tuple accepted by the closed consumer contract.
  for (const failure of [...payload.failures].reverse()) {
    if (bytes.length <= MAX_EPHEMERAL_FAILURE_REPORT_BYTES) break;
    if (failure.source_file !== null || failure.source_line !== null || failure.source_column !== null) {
      failure.source_file = null;
      failure.source_line = null;
      failure.source_column = null;
      failure.source_kind = "unavailable";
      bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
    }
  }
  // Route-level image diagnostics are optional private detail. If a stress
  // failure report approaches its cap, preserve each failed test identity and
  // mark these observations unavailable instead of dropping arbitrary fields.
  for (const failure of [...payload.failures].reverse()) {
    if (bytes.length <= MAX_EPHEMERAL_FAILURE_REPORT_BYTES) break;
    if (failure.image_inventory_state === "captured") {
      failure.image_inventory_state = "unavailable";
      failure.image_inventory = null;
      bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
    }
  }
  // Keep at least one event globally for durable diagnostics. Earlier passes
  // reduce each attempt to one event; this pass removes later events only if
  // source compaction still leaves the report over the cap.
  let retainedPageErrorEvents = payload.failures.reduce(
    (total, failure) => total + failure.page_errors.length,
    0,
  );
  for (const failure of [...payload.failures].reverse()) {
    if (bytes.length <= MAX_EPHEMERAL_FAILURE_REPORT_BYTES) break;
    if (failure.page_errors.length > 0 && retainedPageErrorEvents > 1) {
      failure.page_errors.pop();
      retainedPageErrorEvents -= 1;
      failure.page_errors_truncated = true;
      if (failure.page_errors.length === 0) failure.page_error_state = "unavailable";
      bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
    }
  }
  return bytes.length <= MAX_EPHEMERAL_FAILURE_REPORT_BYTES ? bytes : null;
}

function writeBoundedFailureSummary(
  binding,
  suite,
  summary,
  { gateRoot = GATE_ROOT, sourceRoot = process.cwd() } = {},
) {
  if (!binding || !summary || !suite || typeof suite.allTests !== "function") return false;
  const gateName = path.basename(binding.gate);
  if (
    path.dirname(binding.gate) !== gateRoot
    || !PUBLIC_GATE_NAME.test(gateName)
    || !RUN_STATUSES.has(summary.run_status)
  ) return false;
  const tests = suite.allTests();
  if (
    !Array.isArray(tests)
    || tests.length > MAX_TESTS
    || !summary.logical
    || !publicCoverageIsComplete(tests, summary.logical)
  ) return false;
  const failures = [];
  let attempts = 0;
  for (const test of tests) {
    if (!test || !Array.isArray(test.results)) return false;
    for (const result of test.results) {
      attempts += 1;
      if (attempts > MAX_ATTEMPTS) return false;
      if (FAILURE_ATTEMPT_STATUSES.has(result?.status)) {
        const record = failureRecordForAttempt(test, result, attemptsForTest(test, result), sourceRoot);
        if (!record) return false;
        failures.push(record);
      }
    }
  }
  const failureExpected = summary.run_status !== "passed"
    || summary.logical.fail > 0
    || summary.logical.flaky > 0
    || summary.logical.interrupted > 0
    || summary.attempts.fail > 0
    || summary.attempts.interrupted > 0
    || summary.attempts.timedout > 0;
  if (!failureExpected) return false;

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
    || !gateMetadata.isDirectory()
    || gateMetadata.isSymbolicLink()
    || gateMetadata.uid !== process.geteuid()
    || (gateMetadata.mode & 0o777) !== 0o700
    || !resultsMetadata.isDirectory()
    || resultsMetadata.isSymbolicLink()
    || resultsMetadata.uid !== process.geteuid()
    || (resultsMetadata.mode & 0o777) !== 0o700
  ) return false;

  const payload = {
    schema: 2,
    gate: gateName,
    source_sha: binding.source_sha,
    app_sha: binding.app_sha,
    marker_sha256: binding.marker_sha256,
    run_status: summary.run_status,
    failures,
  };
  const bytes = boundedFailureReportBytes(payload);
  if (!bytes) return false;
  const output = path.join(binding.gate, "test-results", FAILURE_REPORT_FILE);
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
    ) return false;
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
    return true;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function writeBoundedFailureIdentitySummary(
  binding,
  suite,
  summary,
  { gateRoot = GATE_ROOT } = {},
) {
  if (!binding || !summary || !suite || typeof suite.allTests !== "function") return false;
  const gateName = path.basename(binding.gate);
  if (
    path.dirname(binding.gate) !== gateRoot
    || !PUBLIC_GATE_NAME.test(gateName)
    || !RUN_STATUSES.has(summary.run_status)
    || !SHA40.test(binding.source_sha || "")
    || !SHA40.test(binding.app_sha || "")
    || !SHA64.test(binding.marker_sha256 || "")
  ) return false;
  const tests = suite.allTests();
  if (
    !Array.isArray(tests)
    || tests.length > MAX_TESTS
    || !summary.logical
    || !publicCoverageIsComplete(tests, summary.logical)
  ) return false;

  const failures = [];
  let attempts = 0;
  for (const test of tests) {
    if (!test || !Array.isArray(test.results)) return false;
    const project = typeof test.parent?.project === "function"
      ? test.parent.project()?.name
      : undefined;
    const id = typeof test.title === "string" ? PUBLIC_TEST_IDS.get(test.title) : undefined;
    if (!id || !PUBLIC_PROJECTS.has(project)) return false;
    for (const result of test.results) {
      attempts += 1;
      if (attempts > MAX_ATTEMPTS) return false;
      if (!FAILURE_ATTEMPT_STATUSES.has(result?.status)) continue;
      failures.push({ id, project, status: result.status, attempt: attemptsForTest(test, result) });
      if (failures.length > MAX_FAILURE_IDENTITY_RECORDS) return false;
    }
  }
  if (failures.length === 0) return false;

  let rootMetadata;
  let gateMetadata;
  let resultsMetadata;
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
    || !gateMetadata.isDirectory()
    || gateMetadata.isSymbolicLink()
    || gateMetadata.uid !== process.geteuid()
    || (gateMetadata.mode & 0o777) !== 0o700
    || !resultsMetadata.isDirectory()
    || resultsMetadata.isSymbolicLink()
    || resultsMetadata.uid !== process.geteuid()
    || (resultsMetadata.mode & 0o777) !== 0o700
  ) return false;

  const payload = {
    schema: 1,
    gate: gateName,
    source_sha: binding.source_sha,
    app_sha: binding.app_sha,
    marker_sha256: binding.marker_sha256,
    run_status: summary.run_status,
    failures,
  };
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "ascii");
  if (bytes.length > MAX_FAILURE_IDENTITY_REPORT_BYTES) return false;
  const output = path.join(binding.gate, "test-results", FAILURE_IDENTITY_REPORT_FILE);
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
    ) return false;
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
    return true;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function attemptsForTest(test, result) {
  return test.results.indexOf(result) + 1;
}

class LiveCountReporter {
  constructor(options = {}) {
    this.gateRoot = options.gateRoot ?? GATE_ROOT;
    this.sourceRoot = options.sourceRoot ?? process.cwd();
  }

  onBegin(_config, suite) {
    this.suite = suite;
  }

  onEnd(result) {
    const binding = bindingsFromEnvironment(process.env, { gateRoot: this.gateRoot });
    const publicGate = binding !== null && PUBLIC_GATE_NAME.test(path.basename(binding.gate));
    const liveUserGate = binding !== null && LIVE_USER_GATE_NAME.test(path.basename(binding.gate));
    const summary = summarizeRun(this.suite, result?.status, { publicGate });
    if (publicGate && summary) {
      try {
        writeBoundedFailureIdentitySummary(binding, this.suite, summary, {
          gateRoot: this.gateRoot,
        });
      } catch {
        // The independent closed identity record is best effort; it never
        // changes the count result or prevents the richer report attempt.
      }
      try {
        writeBoundedFailureSummary(binding, this.suite, summary, {
          gateRoot: this.gateRoot,
          sourceRoot: this.sourceRoot,
        });
      } catch {
        // The guard emits a closed failure_report_unavailable marker if the
        // optional private record cannot be consumed or persisted.
      }
    }
    if (liveUserGate && summary) {
      try {
        writeBoundedLiveUserFailureSummary(binding, this.suite, summary, {
          gateRoot: this.gateRoot,
          sourceRoot: this.sourceRoot,
        });
      } catch {
        // The guard reports a closed unavailable status if the record cannot
        // be consumed or persisted; the original Playwright result remains.
      }
    }
    if (!writeBoundedSummary(binding, summary, { gateRoot: this.gateRoot })) {
      return { status: "failed" };
    }
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
module.exports.writeBoundedFailureSummary = writeBoundedFailureSummary;
module.exports.writeBoundedFailureIdentitySummary = writeBoundedFailureIdentitySummary;
module.exports.publicFailureIdentityMappingIsComplete = publicFailureIdentityMappingIsComplete;
module.exports.publicTestTitles = publicTestTitles;
module.exports.writeBoundedLiveUserFailureSummary = writeBoundedLiveUserFailureSummary;
module.exports.createPageErrorAttachment = createPageErrorAttachment;
module.exports.createLiveUserApiFailureAttachment = createLiveUserApiFailureAttachment;
module.exports.userApiFailureMessage = userApiFailureMessage;
module.exports.USER_API_OPERATION_CONTRACTS = LIVE_USER_API_OPERATIONS;
module.exports.matchesLiveUserApiOperation = matchesLiveUserApiOperation;
module.exports.trustedSuccessfulNextScriptPath = trustedSuccessfulNextScriptPath;
module.exports.pageErrorClass = pageErrorClass;
module.exports.MAX_PAGE_ERROR_EVENTS = MAX_PAGE_ERROR_EVENTS;
module.exports.MAX_TRACKED_NEXT_SCRIPTS = MAX_TRACKED_NEXT_SCRIPTS;
