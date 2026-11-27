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
  writeBoundedLiveUserFailureSummary,
  createPageErrorAttachment,
  createLiveUserApiFailureAttachment,
  userApiFailureMessage,
  USER_API_OPERATION_CONTRACTS,
  matchesLiveUserApiOperation,
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
  "live four current patch DTOs render owner-scoped hero and ability changes",
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
assert.deepEqual(Object.fromEntries(Object.entries(USER_API_OPERATION_CONTRACTS).map(
  ([operation, contract]) => [operation, {
    method: contract.method,
    expected_status: contract.expected_status,
  }],
)), {
  tournament_create: { method: "POST", expected_status: 201 },
  tournament_status: { method: "PATCH", expected_status: 200 },
  invite_create: { method: "POST", expected_status: 201 },
  invite_claim: { method: "POST", expected_status: 201 },
  tournament_join: { method: "POST", expected_status: 201 },
  ready_check_start: { method: "POST", expected_status: 201 },
  ready_check_vote: { method: "POST", expected_status: 200 },
  ready_check_close: { method: "POST", expected_status: 200 },
  captain_round_start: { method: "POST", expected_status: 201 },
  assignment_start: { method: "POST", expected_status: 202 },
  assignment_state: { method: "GET", expected_status: 200 },
  assignment_publish: { method: "POST", expected_status: 200 },
  assignment_lock: { method: "POST", expected_status: 200 },
  bracket_read: { method: "GET", expected_status: 200 },
  match_schedule: { method: "PATCH", expected_status: 200 },
  tournament_read: { method: "GET", expected_status: 200 },
});
const liveUserOperationExamples = {
  tournament_create: "/tournaments",
  tournament_status: "/tournaments/qa-123/status",
  invite_create: "/tournaments/qa-123/invites",
  invite_claim: "/tournaments/invites/claim",
  tournament_join: "/tournaments/qa-123/join",
  ready_check_start: "/tournaments/qa-123/deadlock/ready-check/start",
  ready_check_vote: "/tournaments/qa-123/deadlock/ready-check/vote",
  ready_check_close: "/tournaments/qa-123/deadlock/ready-check/close",
  captain_round_start: "/tournaments/qa-123/deadlock/captain-round/start",
  assignment_start: "/tournaments/qa-123/deadlock/auto-assignment/run-async",
  assignment_state: "/tournaments/qa-123/deadlock/auto-assignment",
  assignment_publish: "/tournaments/qa-123/deadlock/auto-assignment/run-456/publish",
  assignment_lock: "/tournaments/qa-123/deadlock/auto-assignment/run-456/lock",
  bracket_read: "/tournaments/qa-123/bracket?teams_view=summary",
  match_schedule: "/tournaments/qa-123/matches/match-789/schedule",
  tournament_read: "/tournaments/qa-123",
};
for (const [operation, examplePath] of Object.entries(liveUserOperationExamples)) {
  const contract = USER_API_OPERATION_CONTRACTS[operation];
  assert.equal(
    matchesLiveUserApiOperation(operation, contract.method, examplePath),
    true,
    `${operation} accepts its exact closed route shape`,
  );
}
for (const [operation, method, requestPath] of [
  ["tournament_create", "POST", "/tournaments/qa-123"],
  ["tournament_create", "POST", "/tournaments?token=private"],
  ["bracket_read", "GET", "/tournaments/qa-123/bracket?teams_view=summary&marker=private"],
  ["assignment_state", "POST", "/tournaments/qa-123/deadlock/auto-assignment"],
  ["assignment_state", "GET", "/tournaments/../private"],
  ["unknown_operation", "GET", "/tournaments/qa-123"],
]) {
  assert.equal(
    matchesLiveUserApiOperation(operation, method, requestPath),
    false,
    "unmapped method/path/query combinations cannot be attributed to an operation",
  );
}
const userApiFailureInput = {
  kind: "unexpected_status",
  operation: "assignment_state",
  method: "GET",
  expected_status: 200,
  actual_status: 503,
  source_file: "tests/smoke/live-user-journey.spec.ts",
  source_line: 812,
  source_column: 17,
};
const userApiFailureAttachment = createLiveUserApiFailureAttachment(userApiFailureInput);
assert.equal(userApiFailureAttachment?.name, "live-user-api-failure-v1");
assert.equal(userApiFailureAttachment?.contentType, "application/json");
assert.deepEqual(JSON.parse(userApiFailureAttachment.body.toString("ascii")), {
  schema: 1,
  ...userApiFailureInput,
});
assert.equal(
  userApiFailureMessage("unexpected_status"),
  "LiveUserApiUnexpectedStatus: API response status did not match.",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    method: "POST",
  }),
  null,
  "an operation cannot claim a different HTTP method",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    expected_status: 201,
  }),
  null,
  "an operation cannot claim a different expected status",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    actual_status: 200,
  }),
  null,
  "unexpected_status requires a status different from the expected status",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    operation: "unknown_route_with_secret",
  }),
  null,
  "dynamic routes and unrecognized operation names are rejected",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    source_file: "/tmp/private/tests/smoke/live-user-journey.spec.ts",
  }),
  null,
  "only the normalized repository-relative spec source is accepted",
);
assert.equal(
  createLiveUserApiFailureAttachment({
    ...userApiFailureInput,
    kind: "request_error",
    actual_status: 503,
  }),
  null,
  "request_error has no HTTP status",
);
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
function imageInventoryAttachment(route, values) {
  return {
    name: "live-image-inventory-evidence-v1",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify({
      schema: 1,
      route,
      image_count: values.image_count,
      missing_current_src_count: values.missing_current_src_count,
      unloaded_count: values.unloaded_count,
      complete: values.complete,
      counts_truncated: values.counts_truncated ?? false,
    }), "ascii"),
  };
}
function imageInventoryV2Image(overrides = {}) {
  return {
    ordinal: 1,
    generation: 1,
    src_present: true,
    srcset_present: false,
    complete: true,
    natural_width_positive: true,
    current_src_present: true,
    loading: "eager",
    viewport_state: "intersecting",
    scroll_attempted: true,
    request_state: "request_finished",
    response_received: true,
    response_status_class: "2xx",
    request_finished: true,
    request_failed: false,
    request_failure_class: "none",
    csp_state: "not_observed",
    csp_image_block_count: 0,
    terminal_duration_bucket: "<1s",
    ...overrides,
  };
}
function imageInventoryV2Payload(route, values) {
  return {
    schema: 2,
    route,
    image_count: values.image_count ?? values.images.length,
    missing_current_src_count: values.missing_current_src_count ?? 0,
    unloaded_count: values.unloaded_count ?? 0,
    complete: values.complete ?? false,
    counts_truncated: values.counts_truncated ?? false,
    image_entries_truncated: values.image_entries_truncated ?? false,
    coverage: values.coverage ?? {
      network_state: "captured",
      csp_state: "captured",
      unmatched_csp_count: 0,
      csp_events_truncated: false,
    },
    images: values.images,
  };
}
function imageInventoryV2Attachment(route, values) {
  return {
    name: "live-image-inventory-evidence-v2",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(imageInventoryV2Payload(route, values)), "ascii"),
  };
}
function rawImageInventoryV2Attachment(payload) {
  return {
    name: "live-image-inventory-evidence-v2",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(payload), "ascii"),
  };
}
function browserRequestFailureAttachment({
  schema = 1,
  failure_phase = "hook",
  truncated = false,
  events = [],
} = {}) {
  return {
    name: `live-browser-request-failures-v${schema}`,
    contentType: "application/json",
    body: Buffer.from(JSON.stringify({
      schema,
      coverage: "since_before_each",
      failure_phase,
      truncated,
      events,
    }), "ascii"),
  };
}
function browserRequestFailureEvent(overrides = {}) {
  return {
    ordinal: 1,
    resource_type: "fetch",
    route_scope: "home",
    navigation_relation: "same",
    failure_class: "other",
    duration_bucket: "<1s",
    ...overrides,
  };
}
function browserRequestFailureEventV2(overrides = {}) {
  return {
    ordinal: 1,
    method: "GET",
    resource_type: "fetch",
    route_scope: "home",
    failure_route_scope: "home",
    endpoint_family: "api_content",
    navigation_relation: "same",
    navigation_phase: "none",
    failure_class: "cancelled",
    duration_bucket: "<1s",
    ...overrides,
  };
}
function rawBrowserRequestFailureAttachment(payload) {
  return {
    name: `live-browser-request-failures-v${payload.schema ?? 1}`,
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(payload), "ascii"),
  };
}
function objectiveScreenshotAttachment(overrides = {}) {
  return {
    name: "objective-screenshot-error-v1",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify({
      schema: 1,
      category: "capture_timeout",
      error_name: "TimeoutError",
      phase: "capture",
      error_code: "ETIMEDOUT",
      viewport: { width: 1430, height: 1000 },
      document_height_bucket: "10k-30k",
      elapsed_bucket: ">=30s",
      page_closed: false,
      browser_connected: true,
      crash_observed: false,
      ...overrides,
    }), "ascii"),
  };
}
function navigationFailureAttachment(overrides = {}) {
  return {
    name: "live-navigation-failure-v1",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify({
      schema: 1,
      test_id: "csp_nonce_rotation",
      result_route: "other",
      query_present: false,
      hash_present: false,
      trailing_slash: false,
      click_dispatched: true,
      click_default_prevented: "unavailable",
      document_response: "not_seen",
      info_rsc_response: "2xx",
      info_rsc_response_phase: "during_click",
      info_rsc_response_timing: "<1s",
      main_frame_commit: "not_seen",
      soft_nonce: "changed",
      reload_nonce: "not_read",
      ...overrides,
    }), "ascii"),
  };
}
function decodedImageFailureAttachment(overrides = {}) {
  return {
    name: "decoded-image-failure-v1",
    contentType: "application/json",
    body: Buffer.from(JSON.stringify({
      schema: 1,
      patch_id: "123456",
      role: "ability",
      section_index: 2,
      element_index: 1,
      state: "decode_rejected",
      decode_error: "rejected",
      current_src_present: true,
      current_src_changed: false,
      width_bucket: "zero",
      height_bucket: "zero",
      ...overrides,
    }), "ascii"),
  };
}
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

function liveUserDiagnosticFixture(root, suffix) {
  const gateRoot = path.join(root, `live-user-gate-root-${suffix}`);
  const gate = path.join(gateRoot, "live-user-qa.Abc123");
  const results = path.join(gate, "test-results");
  const sourceRoot = path.join(root, `live-user-source-${suffix}`);
  const spec = path.join(sourceRoot, "tests", "smoke", "live-user-journey.spec.ts");
  fs.mkdirSync(results, { recursive: true, mode: 0o700 });
  fs.chmodSync(gateRoot, 0o711);
  fs.mkdirSync(path.dirname(spec), { recursive: true, mode: 0o755 });
  fs.writeFileSync(spec, 'test("the fixed journey", async () => {\n  expect(true).toBe(true);\n});\n');
  return {
    gateRoot,
    gate,
    results,
    sourceRoot,
    spec,
    binding: {
      gate,
      source_sha: "b".repeat(40),
      app_sha: "a".repeat(40),
      marker_sha256: "c".repeat(64),
    },
  };
}

function liveUserFailureTest(fixture, { status = "failed", attachments } = {}) {
  return {
    title: "live player completes the accelerated tournament journey through visible controls",
    id: "private-playwright-id",
    expectedStatus: "passed",
    parent: { project: () => ({ name: "live-desktop" }) },
    results: [{
      status,
      errors: [{
        message: "private response body and marker must never be serialized",
        location: { file: fixture.spec, line: 2, column: 3 },
      }],
      attachments,
    }],
    outcome: () => "unexpected",
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

const liveUserFailureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "live-user-failure-contract-"));
try {
  const fixture = liveUserDiagnosticFixture(liveUserFailureRoot, "captured");
  const candidate = createLiveUserApiFailureAttachment({
    kind: "unexpected_status",
    operation: "assignment_state",
    method: "GET",
    expected_status: 200,
    actual_status: 503,
    source_file: "tests/smoke/live-user-journey.spec.ts",
    source_line: 2,
    source_column: 3,
  });
  assert.ok(candidate);
  const failedTest = liveUserFailureTest(fixture, { attachments: [candidate] });
  const suite = { allTests: () => [failedTest] };
  const summary = summarizeRun(suite, "failed");
  assert.equal(summary.run_status, "failed");
  assert.equal(summary.logical.fail, 1);
  assert.equal(summary.attempts.fail, 1);
  assert.equal(withRootOwnedGate(fixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
    fixture.binding,
    suite,
    summary,
    { gateRoot: fixture.gateRoot, sourceRoot: fixture.sourceRoot },
  )), true, "a terminal failed UserQA API assertion produces its closed private report");
  const reportPath = path.join(fixture.results, "live-user-failures-v3.json");
  const reportBytes = fs.readFileSync(reportPath);
  const reportMetadata = fs.lstatSync(reportPath);
  const report = JSON.parse(reportBytes.toString("ascii"));
  assert.equal(reportMetadata.isFile(), true);
  assert.equal(reportMetadata.nlink, 1);
  assert.equal(reportMetadata.mode & 0o777, 0o600);
  assert.ok(reportBytes.length <= 16 * 1024 - 256);
  assert.deepEqual(Object.keys(report).sort(), [
    "app_sha", "failures", "gate", "marker_sha256", "run_status", "schema", "source_sha",
  ]);
  assert.equal(report.schema, 3);
  assert.deepEqual(report.failures, [{
    id: "live_user_journey",
    project: "live-desktop",
    status: "failed",
    attempt: 1,
    source_file: "tests/smoke/live-user-journey.spec.ts",
    source_line: 2,
    source_column: 3,
    source_kind: "test_error_location",
    browser_gate_state: "unavailable",
    browser_gate: null,
    api_failure_state: "captured",
    api_failure: {
      kind: "unexpected_status",
      operation: "assignment_state",
      method: "GET",
      expected_status: 200,
      actual_status: 503,
    },
  }]);
  for (const privateValue of [
    "private response body",
    "private marker",
    "private-playwright-id",
    "/tournaments/",
    "live-user-api-failure-v1",
    fixture.sourceRoot,
  ]) {
    assert.equal(reportBytes.includes(Buffer.from(privateValue)), false);
  }

  const nonmatchingFixture = liveUserDiagnosticFixture(liveUserFailureRoot, "nonmatching");
  const nonmatchingCandidate = createLiveUserApiFailureAttachment({
    kind: "unexpected_status",
    operation: "assignment_state",
    method: "GET",
    expected_status: 200,
    actual_status: 503,
    source_file: "tests/smoke/live-user-journey.spec.ts",
    source_line: 2,
    source_column: 3,
  });
  const unrelatedTerminalFailure = liveUserFailureTest(nonmatchingFixture, {
    attachments: [nonmatchingCandidate],
  });
  unrelatedTerminalFailure.results[0].errors[0].location.line = 1;
  const unrelatedSuite = { allTests: () => [unrelatedTerminalFailure] };
  const unrelatedSummary = summarizeRun(unrelatedSuite, "failed");
  assert.equal(withRootOwnedGate(nonmatchingFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
    nonmatchingFixture.binding,
    unrelatedSuite,
    unrelatedSummary,
    { gateRoot: nonmatchingFixture.gateRoot, sourceRoot: nonmatchingFixture.sourceRoot },
  )), true, "a terminal failure is retained even when an earlier helper candidate does not match");
  const unrelatedReport = JSON.parse(fs.readFileSync(
    path.join(nonmatchingFixture.results, "live-user-failures-v3.json"),
    "ascii",
  ));
  assert.deepEqual([
    unrelatedReport.failures[0].source_line,
    unrelatedReport.failures[0].api_failure_state,
    unrelatedReport.failures[0].api_failure,
  ], [1, "unavailable", null]);

  const ambiguousFixture = liveUserDiagnosticFixture(liveUserFailureRoot, "ambiguous");
  const ambiguousTest = liveUserFailureTest(ambiguousFixture, { attachments: [candidate] });
  ambiguousTest.results[0].errors.push({
    message: "another private terminal error",
    location: { file: ambiguousFixture.spec, line: 2, column: 3 },
  });
  const ambiguousSuite = { allTests: () => [ambiguousTest] };
  const ambiguousSummary = summarizeRun(ambiguousSuite, "failed");
  assert.equal(withRootOwnedGate(ambiguousFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
    ambiguousFixture.binding,
    ambiguousSuite,
    ambiguousSummary,
    { gateRoot: ambiguousFixture.gateRoot, sourceRoot: ambiguousFixture.sourceRoot },
  )), true);
  const ambiguousReport = JSON.parse(fs.readFileSync(
    path.join(ambiguousFixture.results, "live-user-failures-v3.json"),
    "ascii",
  ));
  assert.deepEqual([
    ambiguousReport.failures[0].source_kind,
    ambiguousReport.failures[0].api_failure_state,
    ambiguousReport.failures[0].api_failure,
  ], ["test_error_location", "unavailable", null]);

  const unavailableCases = [
    ["missing attachment", undefined],
    ["wrong content type", [{ ...candidate, contentType: "text/plain" }]],
    ["path-backed attachment", [{ name: candidate.name, contentType: candidate.contentType, path: "/private/path" }]],
    ["body and path", [{ ...candidate, path: "/private/path" }]],
    ["duplicate attachment", [candidate, candidate]],
    ["malformed JSON", [{ ...candidate, body: Buffer.from("{", "ascii") }]],
    ["noncanonical schema boolean", [{
      ...candidate,
      body: Buffer.from(JSON.stringify({
        schema: true,
        kind: "unexpected_status",
        operation: "assignment_state",
        method: "GET",
        expected_status: 200,
        actual_status: 503,
        source_file: "tests/smoke/live-user-journey.spec.ts",
        source_line: 2,
        source_column: 3,
      }), "ascii"),
    }]],
    ["unknown attachment field", [{
      ...candidate,
      body: Buffer.from(JSON.stringify({
        schema: 1,
        kind: "unexpected_status",
        operation: "assignment_state",
        method: "GET",
        expected_status: 200,
        actual_status: 503,
        source_file: "tests/smoke/live-user-journey.spec.ts",
        source_line: 2,
        source_column: 3,
        private_value: "must-not-survive",
      }), "ascii"),
    }]],
  ];
  for (const [index, [label, attachments]] of unavailableCases.entries()) {
    const unsafeFixture = liveUserDiagnosticFixture(
      liveUserFailureRoot,
      `invalid-${index}`,
    );
    const unsafeTest = liveUserFailureTest(unsafeFixture, { attachments });
    const unsafeSuite = { allTests: () => [unsafeTest] };
    const unsafeSummary = summarizeRun(unsafeSuite, "failed");
    assert.equal(withRootOwnedGate(unsafeFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
      unsafeFixture.binding,
      unsafeSuite,
      unsafeSummary,
      { gateRoot: unsafeFixture.gateRoot, sourceRoot: unsafeFixture.sourceRoot },
    )), true, `${label} leaves the original failed result reportable`);
    const unsafeReportBytes = fs.readFileSync(
      path.join(unsafeFixture.results, "live-user-failures-v3.json"),
    );
    const unsafeReport = JSON.parse(unsafeReportBytes.toString("ascii"));
    assert.deepEqual([
      unsafeReport.failures[0].status,
      unsafeReport.failures[0].source_file,
      unsafeReport.failures[0].api_failure_state,
      unsafeReport.failures[0].api_failure,
    ], ["failed", "tests/smoke/live-user-journey.spec.ts", "unavailable", null]);
    for (const privateValue of ["/private/path", "must-not-survive", "private_value"]) {
      assert.equal(unsafeReportBytes.includes(Buffer.from(privateValue)), false);
    }
  }

  const missingLocationFixture = liveUserDiagnosticFixture(liveUserFailureRoot, "no-location");
  const missingLocationTest = liveUserFailureTest(missingLocationFixture, { attachments: [candidate] });
  missingLocationTest.results[0].errors[0].location = {
    file: "/tmp/untrusted/live-user-journey.spec.ts",
    line: 2,
    column: 3,
  };
  const missingLocationSuite = { allTests: () => [missingLocationTest] };
  const missingLocationSummary = summarizeRun(missingLocationSuite, "failed");
  assert.equal(withRootOwnedGate(missingLocationFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
    missingLocationFixture.binding,
    missingLocationSuite,
    missingLocationSummary,
    { gateRoot: missingLocationFixture.gateRoot, sourceRoot: missingLocationFixture.sourceRoot },
  )), true);
  const missingLocationReport = JSON.parse(fs.readFileSync(
    path.join(missingLocationFixture.results, "live-user-failures-v3.json"),
    "ascii",
  ));
  assert.deepEqual([
    missingLocationReport.failures[0].source_file,
    missingLocationReport.failures[0].source_line,
    missingLocationReport.failures[0].source_column,
    missingLocationReport.failures[0].source_kind,
    missingLocationReport.failures[0].api_failure_state,
  ], [null, null, null, "unavailable", "unavailable"]);

  const successfulFixture = liveUserDiagnosticFixture(liveUserFailureRoot, "success");
  const successfulTest = {
    ...liveUserFailureTest(successfulFixture),
    results: [{ status: "passed", errors: [], attachments: [] }],
    outcome: () => "expected",
  };
  const successfulSuite = { allTests: () => [successfulTest] };
  const successfulLiveUserSummary = summarizeRun(successfulSuite, "passed");
  assert.equal(withRootOwnedGate(successfulFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
    successfulFixture.binding,
    successfulSuite,
    successfulLiveUserSummary,
    { gateRoot: successfulFixture.gateRoot, sourceRoot: successfulFixture.sourceRoot },
  )), false, "a passing run does not create a failure-only UserQA report");
  assert.equal(fs.existsSync(path.join(successfulFixture.results, "live-user-failures-v3.json")), false);

  for (const [suffix, mutate] of [
    ["wrong-title", (testCase) => { testCase.title = "unmapped private title"; }],
    ["wrong-project", (testCase) => { testCase.parent = { project: () => ({ name: "live-mobile" }) }; }],
  ]) {
    const rejectedFixture = liveUserDiagnosticFixture(liveUserFailureRoot, suffix);
    const rejectedTest = liveUserFailureTest(rejectedFixture, { attachments: [candidate] });
    mutate(rejectedTest);
    const rejectedSuite = { allTests: () => [rejectedTest] };
    const rejectedSummary = summarizeRun(rejectedSuite, "failed");
    assert.equal(withRootOwnedGate(rejectedFixture.gateRoot, () => writeBoundedLiveUserFailureSummary(
      rejectedFixture.binding,
      rejectedSuite,
      rejectedSummary,
      { gateRoot: rejectedFixture.gateRoot, sourceRoot: rejectedFixture.sourceRoot },
    )), false, "only the fixed UserQA test and project can produce this private report");
    assert.equal(
      fs.existsSync(path.join(rejectedFixture.results, "live-user-failures-v3.json")),
      false,
    );
  }
} finally {
  fs.rmSync(liveUserFailureRoot, { recursive: true, force: true });
}

function failureTest(
  fixture,
  {
    title = publicTitles[6],
    project = "live-desktop",
    errorLocation,
    omitSourceLocation = false,
    attachments,
    results,
  } = {},
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
    location: omitSourceLocation ? undefined : { file: fixture.spec, line: 1, column: 1 },
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

function assertProducerRoundTrip({
  root,
  suffix,
  imageFixturePath,
  requestFixturePath,
  expectedUnloaded,
  expectedFailurePhase,
}) {
  const imageMetadata = fs.lstatSync(imageFixturePath);
  assert.ok(imageMetadata.isFile() && !imageMetadata.isSymbolicLink());
  assert.equal(imageMetadata.uid, process.geteuid());
  assert.equal(imageMetadata.mode & 0o777, 0o600);
  const imageBody = fs.readFileSync(imageFixturePath);
  assert.ok(imageBody.length > 0 && imageBody.length <= 8 * 1024);
  const imagePayload = JSON.parse(imageBody.toString("ascii"));
  assert.equal(imagePayload.schema, 2);
  assert.equal(imagePayload.route, "home");
  assert.equal(imagePayload.image_count, 10);
  assert.equal(imagePayload.unloaded_count, expectedUnloaded);
  assert.equal(imagePayload.images.length, 10);
  assert.equal(imagePayload.images.filter((image) => (
    !image.complete || !image.natural_width_positive
  )).length, expectedUnloaded);

  const attachments = [{
    name: "live-image-inventory-evidence-v2",
    contentType: "application/json",
    body: imageBody,
  }];
  if (requestFixturePath) {
    const requestMetadata = fs.lstatSync(requestFixturePath);
    assert.ok(requestMetadata.isFile() && !requestMetadata.isSymbolicLink());
    assert.equal(requestMetadata.uid, process.geteuid());
    assert.equal(requestMetadata.mode & 0o777, 0o600);
    const requestBody = fs.readFileSync(requestFixturePath);
    assert.ok(requestBody.length > 0 && requestBody.length <= 4 * 1024);
    const requestPayload = JSON.parse(requestBody.toString("ascii"));
    assert.equal(requestPayload.failure_phase, expectedFailurePhase);
    attachments.push({
      name: "live-browser-request-failures-v1",
      contentType: "application/json",
      body: requestBody,
    });
  }

  const fixture = diagnosticFixture(root, suffix);
  const failure = failureTest(fixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    omitSourceLocation: true,
    attachments,
  });
  const plan = publicPlanWithFailure(failure);
  const runSummary = summarizeRun({ allTests: () => plan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(fixture.gateRoot, () => writeBoundedSummary(
    fixture.binding,
    runSummary,
    { gateRoot: fixture.gateRoot },
  )), true, "the normal public-count writer records the matching failure totals");
  assert.equal(withRootOwnedGate(fixture.gateRoot, () => writeBoundedFailureSummary(
    fixture.binding,
    { allTests: () => plan },
    runSummary,
    { gateRoot: fixture.gateRoot, sourceRoot: fixture.sourceRoot },
  )), true);
  const reportPath = path.join(fixture.results, "live-failures-v2.json");
  const reportBytes = fs.readFileSync(reportPath);
  const report = JSON.parse(reportBytes.toString("ascii"));
  const record = report.failures[0];
  assert.equal(record.image_inventory_state, "captured");
  assert.deepEqual(record.image_inventory, { schema: 2, routes: [imagePayload] });
  if (requestFixturePath) {
    assert.equal(record.browser_request_failure_state, "captured");
  } else {
    assert.equal(Object.hasOwn(record, "browser_request_failure_state"), false);
    assert.equal(Object.hasOwn(record, "browser_request_failures"), false);
  }
  if (requestFixturePath) {
    assert.equal(record.browser_request_failures.failure_phase, expectedFailurePhase);
    assert.equal(
      record.browser_request_failures.events.length,
      ["hook", "both"].includes(expectedFailurePhase) ? 1 : 0,
    );
  }
  assert.ok(reportBytes.length <= 16 * 1024 - 256);
  for (const privateValue of ["https://", "private.host", "private-alt", "private-src"]) {
    assert.equal(reportBytes.includes(Buffer.from(privateValue)), false);
  }
  const guardValidation = validateEphemeralAndRetainedBrowserReportThroughGuard(reportPath);
  assert.equal(guardValidation.status, 0, guardValidation.stderr);
}

function validateEphemeralAndRetainedBrowserReportThroughGuard(reportPath) {
  const guardPath = path.resolve(__dirname, "../../../../tools/platform_live_qa_guard.py");
  const countsPath = path.join(path.dirname(reportPath), "live-counts-v1.json");
  const python = [
    "import copy, importlib.util, json, os, pathlib, shutil, stat, sys, tempfile, types",
    "guard_path = pathlib.Path(sys.argv[1])",
    "sys.path.insert(0, str(guard_path.parent))",
    "spec = importlib.util.spec_from_file_location('platform_live_qa_guard', guard_path)",
    "guard = importlib.util.module_from_spec(spec)",
    "sys.modules[spec.name] = guard",
    "spec.loader.exec_module(guard)",
    "guard.liveqa_identity = lambda: (os.geteuid(), os.getegid())",
    "report_path = pathlib.Path(sys.argv[2])",
    "ephemeral = json.loads(report_path.read_bytes())",
    "counts = json.loads(pathlib.Path(sys.argv[3]).read_bytes())",
    "gate = report_path.parent.parent",
    "assert gate.name == ephemeral['gate']",
    "guard.RUN_GATE_ROOT = gate.parent",
    "original_path_lstat = pathlib.Path.lstat",
    "original_os_open, original_os_fstat, original_os_stat, original_os_fchown = os.open, os.fstat, os.stat, os.fchown",
    "private_store = pathlib.Path(tempfile.mkdtemp(prefix='live-qa-private-store-', dir=gate.parent))",
    "private_store.chmod(0o700)",
    "private_store_fd = original_os_open(private_store, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))",
    "private_file_fds = set()",
    "def same_private_store(fd):",
    "    try:",
    "        return original_os_fstat(fd).st_ino == original_os_fstat(private_store_fd).st_ino",
    "    except OSError:",
    "        return False",
    "def contract_os_open(path, flags, mode=0o777, *, dir_fd=None):",
    "    fd = original_os_open(path, flags, mode, dir_fd=dir_fd)",
    "    if dir_fd is not None and same_private_store(dir_fd) and flags & os.O_CREAT:",
    "        private_file_fds.add(fd)",
    "    return fd",
    "def contract_os_fstat(fd):",
    "    value = original_os_fstat(fd)",
    "    if fd in private_file_fds:",
    "        return types.SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_gid=0, st_nlink=value.st_nlink, st_size=value.st_size)",
    "    return value",
    "def contract_os_stat(path, *, dir_fd=None, follow_symlinks=True):",
    "    value = original_os_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)",
    "    if dir_fd is not None and same_private_store(dir_fd) and str(path).endswith('.failure-v2.json'):",
    "        return types.SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_gid=0, st_nlink=value.st_nlink, st_size=value.st_size)",
    "    return value",
    "def contract_path_lstat(self, *args, **kwargs):",
    "    if self == guard.RUN_GATE_ROOT:",
    "        return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o711, st_uid=0, st_gid=0)",
    "    return original_path_lstat(self, *args, **kwargs)",
    "guard._read_active_failure_manifest = lambda _sha: {'manifest_sha256': 'd' * 64, 'payload_tree_sha256': 'e' * 64}",
    "guard._failure_report_store_directory = lambda create: os.dup(private_store_fd)",
    "guard._assert_failure_report_name_unused = lambda _gate: None",
    "guard._prune_browser_failure_diagnostics = lambda reserve_slots: None",
    "os.open, os.fstat, os.stat, os.fchown = contract_os_open, contract_os_fstat, contract_os_stat, lambda _fd, _uid, _gid: None",
    "pathlib.Path.lstat = contract_path_lstat",
    "try:",
    "    public_counts_line = guard.public_browser_counts_line(",
    "        gate, source_sha=ephemeral['source_sha'], app_sha=ephemeral['app_sha'],",
    "        marker_sha256=ephemeral['marker_sha256'], persist_failure_report=True,",
    "    )",
    "    assert 'run_status=failed' in public_counts_line and 'attempt_fail=1' in public_counts_line",
    "    retained_path = private_store / (gate.name + '.failure-v2.json')",
    "    assert retained_path.is_file() and not (private_store / (gate.name + '.failure-v4.json')).exists()",
    "    retained_bytes = retained_path.read_bytes()",
    "    guard._validate_retained_failure_report(gate.name + '.failure-v2.json', retained_bytes)",
    "    retained = json.loads(retained_bytes)",
    "    assert retained['schema'] == 2",
    "    assert retained['failures'][0]['id'] == ephemeral['failures'][0]['id']",
    "    for key in ('image_inventory_state', 'image_inventory', 'browser_request_failure_state', 'browser_request_failures', 'objective_screenshot_state', 'objective_screenshot'):",
    "        if key in ephemeral['failures'][0]: assert retained['failures'][0][key] == ephemeral['failures'][0][key]",
    "finally:",
    "    pathlib.Path.lstat = original_path_lstat",
    "    os.open, os.fstat, os.stat, os.fchown = original_os_open, original_os_fstat, original_os_stat, original_os_fchown",
    "    os.close(private_store_fd)",
    "    shutil.rmtree(private_store)",
    "manifest = {}",
    "guard.liveqa_identity = lambda: (os.geteuid(), os.getegid())",
    "if 'browser_request_failures' in ephemeral['failures'][0]:",
    "    bad_pair = 'browser_request_failures'",
    "elif 'objective_screenshot' in ephemeral['failures'][0]:",
    "    bad_pair = 'objective_screenshot'",
    "else:",
    "    bad_pair = None",
    "bad_cases = {'wrong_gate': lambda record: record.update({'id': 'csp_negative_probes'})}",
    "if bad_pair == 'browser_request_failures':",
    "    bad_cases.update({'missing_pair_member': lambda record: record.pop(bad_pair), 'malformed_pair': lambda record: record[bad_pair].update({'extra': True})})",
    "elif bad_pair == 'objective_screenshot':",
    "    bad_cases.update({'missing_pair_member': lambda record: record.pop(bad_pair), 'malformed_pair': lambda record: record.update({'objective_screenshot_state': 'invalid'})})",
    "temporary_gates = []",
    "try:",
    "    for label, mutate in bad_cases.items():",
    "        bad_gate = gate.parent / ('public-live-qa.' + label + '1234abcd')",
    "        temporary_gates.append(bad_gate)",
    "        bad_results = bad_gate / 'test-results'",
    "        bad_results.mkdir(parents=True, mode=0o700)",
    "        bad_gate.chmod(0o700)",
    "        bad_payload = copy.deepcopy(ephemeral)",
    "        bad_payload['gate'] = bad_gate.name",
    "        mutate(bad_payload['failures'][0])",
    "        target = bad_results / guard.LIVE_BROWSER_FAILURES_FILE",
    "        target.write_text(json.dumps(bad_payload, separators=(',', ':')) + '\\n', encoding='ascii')",
    "        target.chmod(0o600)",
    "        try:",
    "            guard._read_ephemeral_failure_report(",
    "                bad_gate, source_sha=ephemeral['source_sha'], app_sha=ephemeral['app_sha'],",
    "                marker_sha256=ephemeral['marker_sha256'], counts=counts, manifest=manifest,",
    "            )",
    "        except guard.GuardError:",
    "            pass",
    "        else:",
    "            raise AssertionError('ephemeral browser evidence accepted invalid case: ' + label + ' / ' + str(bad_pair) + ' / ' + ','.join(sorted(bad_payload['failures'][0])))",
    "finally:",
    "    for bad_gate in temporary_gates:",
    "        shutil.rmtree(bad_gate, ignore_errors=True)",
  ].join("\n");
  return spawnSync("python3", ["-I", "-B", "-c", python, guardPath, reportPath, countsPath], {
    encoding: "utf8",
    timeout: 5_000,
  });
}

function validateRetainedBrowserReportThroughGuard(reportPath) {
  const guardPath = path.resolve(__dirname, "../../../../tools/platform_live_qa_guard.py");
  const python = [
    "import importlib.util, json, pathlib, sys",
    "guard_path = pathlib.Path(sys.argv[1])",
    "sys.path.insert(0, str(guard_path.parent))",
    "spec = importlib.util.spec_from_file_location('platform_live_qa_guard', guard_path)",
    "guard = importlib.util.module_from_spec(spec)",
    "sys.modules[spec.name] = guard",
    "spec.loader.exec_module(guard)",
    "ephemeral = json.loads(pathlib.Path(sys.argv[2]).read_bytes())",
    "retained = {",
    "    'schema': 2, 'gate': ephemeral['gate'],",
    "    'source_sha': ephemeral['source_sha'], 'app_sha': ephemeral['app_sha'],",
    "    'marker_sha256': ephemeral['marker_sha256'],",
    "    'payload_manifest_sha256': 'd' * 64, 'payload_tree_sha256': 'e' * 64,",
    "    'observed_at': '2026-11-23T00:00:00Z',",
    "    'run_status': ephemeral['run_status'], 'failures': ephemeral['failures'],",
    "}",
    "guard._validate_retained_failure_report(",
    "    f\"{ephemeral['gate']}.failure-v2.json\",",
    "    json.dumps(retained, separators=(',', ':')).encode('ascii'),",
    ")",
  ].join("\n");
  return spawnSync("python3", ["-I", "-B", "-c", python, guardPath, reportPath], {
    encoding: "utf8",
    timeout: 5_000,
  });
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
    attachments: [
      pageErrorAttachment,
      imageInventoryAttachment("home", {
        image_count: 8,
        missing_current_src_count: 0,
        unloaded_count: 0,
        complete: true,
      }),
      imageInventoryAttachment("patch", {
        image_count: 5,
        missing_current_src_count: 0,
        unloaded_count: 0,
        complete: true,
      }),
      imageInventoryAttachment("tournaments", {
        image_count: 1,
        missing_current_src_count: 1,
        unloaded_count: 1,
        complete: false,
      }),
    ],
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
    image_inventory_state: "captured",
    image_inventory: {
      schema: 1,
      routes: [
        {
          schema: 1,
          route: "home",
          image_count: 8,
          missing_current_src_count: 0,
          unloaded_count: 0,
          complete: true,
          counts_truncated: false,
        },
        {
          schema: 1,
          route: "patch",
          image_count: 5,
          missing_current_src_count: 0,
          unloaded_count: 0,
          complete: true,
          counts_truncated: false,
        },
        {
          schema: 1,
          route: "tournaments",
          image_count: 1,
          missing_current_src_count: 1,
          unloaded_count: 1,
          complete: false,
          counts_truncated: false,
        },
      ],
    },
  }]);
  assert.equal(reportBytes.includes(Buffer.from("private assertion text")), false);
  assert.equal(reportBytes.includes(Buffer.from("private-playwright-id")), false);
  assert.equal(reportBytes.includes(Buffer.from("private title")), false);
  assert.equal(reportBytes.includes(Buffer.from(failureRoot)), false);
  assert.equal(reportBytes.includes(Buffer.from("abcdefghijklm.js:12:34")), false);
  assert.equal(reportBytes.includes(Buffer.from("https://")), false);
  assert.equal(reportBytes.includes(Buffer.from("alt")), false);
  assert.equal(reportBytes.includes(Buffer.from("currentSrc")), false);

  const v2Images = [
    imageInventoryV2Image({
      ordinal: 1,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      request_state: "pending",
      response_received: false,
      response_status_class: "none",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      terminal_duration_bucket: "<100ms",
    }),
    imageInventoryV2Image({
      ordinal: 2,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      request_state: "pending",
      response_received: false,
      response_status_class: "none",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      terminal_duration_bucket: "<1s",
    }),
    imageInventoryV2Image({
      ordinal: 3,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      viewport_state: "outside",
      request_state: "pending",
      response_received: false,
      response_status_class: "none",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      terminal_duration_bucket: "<5s",
    }),
    imageInventoryV2Image({
      ordinal: 4,
      complete: false,
      natural_width_positive: false,
      loading: "unknown",
      request_state: "pending",
      response_received: false,
      response_status_class: "none",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      terminal_duration_bucket: ">=5s",
    }),
    imageInventoryV2Image({
      ordinal: 5,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      request_state: "response_received",
      response_received: true,
      response_status_class: "2xx",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      terminal_duration_bucket: "<1s",
    }),
    imageInventoryV2Image({
      ordinal: 6,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      request_state: "request_failed",
      response_received: true,
      response_status_class: "2xx",
      request_finished: false,
      request_failed: true,
      request_failure_class: "connection",
      terminal_duration_bucket: "<5s",
    }),
    imageInventoryV2Image({
      ordinal: 7,
      complete: false,
      natural_width_positive: false,
      current_src_present: false,
      loading: "lazy",
      viewport_state: "unknown",
      request_state: "ambiguous",
      response_received: null,
      response_status_class: "ambiguous",
      request_finished: null,
      request_failed: null,
      request_failure_class: "ambiguous",
      csp_state: "ambiguous",
      csp_image_block_count: 0,
      terminal_duration_bucket: "ambiguous",
    }),
    imageInventoryV2Image({
      ordinal: 8,
      complete: false,
      natural_width_positive: false,
      loading: "lazy",
      request_state: "not_observed",
      response_received: false,
      response_status_class: "none",
      request_finished: false,
      request_failed: false,
      request_failure_class: "none",
      csp_state: "matched",
      csp_image_block_count: 1,
      terminal_duration_bucket: "not_observed",
    }),
  ];
  const v2Values = {
    image_count: 8,
    missing_current_src_count: 1,
    unloaded_count: 8,
    complete: false,
    images: v2Images,
    coverage: {
      network_state: "captured",
      csp_state: "captured",
      unmatched_csp_count: 1,
      csp_events_truncated: false,
    },
  };
  const v2Fixture = diagnosticFixture(failureRoot, "v2a2b3c4");
  const v2Attachment = imageInventoryV2Attachment("home", v2Values);
  const v2Failure = failureTest(v2Fixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    errorLocation: { file: v2Fixture.helper, line: 2, column: 3 },
    attachments: [pageErrorAttachment, v2Attachment],
  });
  const v2Plan = publicPlanWithFailure(v2Failure);
  const v2Summary = summarizeRun({ allTests: () => v2Plan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(v2Fixture.gateRoot, () => writeBoundedFailureSummary(
    v2Fixture.binding,
    { allTests: () => v2Plan },
    v2Summary,
    { gateRoot: v2Fixture.gateRoot, sourceRoot: v2Fixture.sourceRoot },
  )), true, "the closed per-image v2 attachment is retained for a failed image test");
  const v2ReportPath = path.join(v2Fixture.results, "live-failures-v2.json");
  const v2ReportBytes = fs.readFileSync(v2ReportPath);
  const v2Report = JSON.parse(v2ReportBytes.toString("ascii"));
  const v2FailureRecord = v2Report.failures[0];
  assert.equal(v2FailureRecord.image_inventory_state, "captured");
  assert.deepEqual(v2FailureRecord.image_inventory, {
    schema: 2,
    routes: [imageInventoryV2Payload("home", v2Values)],
  });
  assert.deepEqual(
    v2FailureRecord.image_inventory.routes[0].images.map((image) => image.terminal_duration_bucket),
    ["<100ms", "<1s", "<5s", ">=5s", "<1s", "<5s", "ambiguous", "not_observed"],
    "pending age is stored only in the four closed coarse buckets",
  );
  assert.equal(v2FailureRecord.image_inventory.routes[0].images[5].response_status_class, "2xx");
  assert.equal(v2FailureRecord.image_inventory.routes[0].images[5].request_failed, true);
  assert.equal(v2FailureRecord.image_inventory.routes[0].images[5].request_failure_class, "connection");
  assert.equal(v2FailureRecord.image_inventory.routes[0].images[6].request_state, "ambiguous");
  assert.equal(v2FailureRecord.image_inventory.routes[0].images[7].csp_state, "matched");
  for (const privateValue of [
    "https://", "private.host", "private-alt", "private-src", "private-source",
  ]) {
    assert.equal(v2ReportBytes.includes(Buffer.from(privateValue)), false);
  }

  const bodyImageFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_BODY_FIXTURE;
  const bodyRequestFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_BODY_REQUEST_FIXTURE;
  if (bodyImageFixture) {
    assertProducerRoundTrip({
      root: failureRoot,
      suffix: "r1a2b3c4",
      imageFixturePath: bodyImageFixture,
      requestFixturePath: bodyRequestFixture,
      expectedUnloaded: 1,
      expectedFailurePhase: "body",
    });
  }
  const hookImageFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_HOOK_FIXTURE;
  const hookRequestFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_REQUEST_FAILURE_FIXTURE;
  if (hookImageFixture) {
    assert.ok(hookRequestFixture, "the hook-only fixture requires its request-failure attachment");
    assertProducerRoundTrip({
      root: failureRoot,
      suffix: "r2a2b3c4",
      imageFixturePath: hookImageFixture,
      requestFixturePath: hookRequestFixture,
      expectedUnloaded: 0,
      expectedFailurePhase: "hook",
    });
  }
  const bothImageFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_BOTH_FIXTURE;
  const bothRequestFixture = process.env.PLATFORM_WEB_IMAGE_INVENTORY_BOTH_REQUEST_FIXTURE;
  if (bothImageFixture) {
    assert.ok(bothRequestFixture, "the combined-failure fixture requires its request-failure attachment");
    assertProducerRoundTrip({
      root: failureRoot,
      suffix: "r3a2b3c4",
      imageFixturePath: bothImageFixture,
      requestFixturePath: bothRequestFixture,
      expectedUnloaded: 1,
      expectedFailurePhase: "both",
    });
  }

  const v2PublicCountsPath = path.join(v2Fixture.results, "live-counts-v1.json");
  assert.equal(withRootOwnedGate(v2Fixture.gateRoot, () => writeBoundedSummary(
    v2Fixture.binding,
    v2Summary,
    { gateRoot: v2Fixture.gateRoot },
  )), true);
  const v2PublicCountsBytes = fs.readFileSync(v2PublicCountsPath);
  const v2PublicCounts = JSON.parse(v2PublicCountsBytes.toString("ascii"));
  assert.equal(v2PublicCounts.run_status, "failed");
  assert.deepEqual(Object.keys(v2PublicCounts).sort(), [
    "app_sha", "attempt_fail", "attempt_interrupted", "attempt_pass",
    "attempt_skip", "attempt_timedout", "attempt_total", "logical_fail",
    "logical_expected_fail", "logical_flaky", "logical_interrupted", "logical_pass",
    "logical_skip", "logical_total", "marker_sha256", "run_status", "schema", "source_sha",
  ].sort());
  for (const privateValue of [
    "image_inventory", "request_state", "csp_image_block_count", "lazy", "connection",
  ]) {
    assert.equal(v2PublicCountsBytes.includes(Buffer.from(privateValue)), false);
  }

  const prioritizedOrdinals = [2, 1, ...Array.from({ length: 14 }, (_, index) => index + 3)];
  const prioritizedImages = prioritizedOrdinals.map((ordinal) => {
    const failed = ordinal === 2;
    return imageInventoryV2Image({
      ordinal,
      complete: !failed,
      natural_width_positive: !failed,
      loading: "lazy",
      request_state: "unavailable",
      response_received: null,
      response_status_class: "unavailable",
      request_finished: null,
      request_failed: null,
      request_failure_class: "unavailable",
      csp_state: "unavailable",
      csp_image_block_count: 0,
      terminal_duration_bucket: "unavailable",
    });
  });
  const priorityAttachment = imageInventoryV2Attachment("home", {
    image_count: 512,
    missing_current_src_count: 0,
    unloaded_count: 1,
    complete: false,
    counts_truncated: true,
    image_entries_truncated: true,
    coverage: {
      network_state: "truncated",
      csp_state: "truncated",
      unmatched_csp_count: 64,
      csp_events_truncated: true,
    },
    images: prioritizedImages,
  });
  assert.ok(priorityAttachment.body.length <= 8 * 1024);
  const priorityFixture = diagnosticFixture(failureRoot, "v2p2b3c4");
  const priorityFailure = failureTest(priorityFixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    errorLocation: { file: priorityFixture.helper, line: 2, column: 3 },
    attachments: [priorityAttachment],
  });
  const priorityPlan = publicPlanWithFailure(priorityFailure);
  assert.equal(withRootOwnedGate(priorityFixture.gateRoot, () => writeBoundedFailureSummary(
    priorityFixture.binding,
    { allTests: () => priorityPlan },
    summarizeRun({ allTests: () => priorityPlan }, "failed", { publicGate: true }),
    { gateRoot: priorityFixture.gateRoot, sourceRoot: priorityFixture.sourceRoot },
  )), true);
  const priorityReport = JSON.parse(fs.readFileSync(
    path.join(priorityFixture.results, "live-failures-v2.json"),
    "ascii",
  ));
  assert.equal(priorityReport.failures[0].image_inventory_state, "captured");
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].images.length, 16);
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].images[0].ordinal, 2);
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].images[0].complete, false);
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].images[0].natural_width_positive, false);
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].images[0].request_state, "unavailable");
  assert.equal(priorityReport.failures[0].image_inventory.routes[0].image_entries_truncated, true);

  const capImages = Array.from({ length: 16 }, (_, index) => imageInventoryV2Image({
    ordinal: index + 1,
    complete: false,
    natural_width_positive: false,
    loading: "lazy",
    request_state: "pending",
    response_received: false,
    response_status_class: "none",
    request_finished: false,
    request_failed: false,
    request_failure_class: "none",
    terminal_duration_bucket: ">=5s",
  }));
  const capAttachments = ["home", "patch", "tournaments"].map((route) => (
    imageInventoryV2Attachment(route, {
      image_count: 16,
      unloaded_count: 16,
      complete: false,
      images: capImages,
    })
  ));
  assert.ok(capAttachments.every((attachment) => attachment.body.length <= 8 * 1024));
  const capFixture = diagnosticFixture(failureRoot, "v2c2b3c4");
  const capFailure = failureTest(capFixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    errorLocation: { file: capFixture.helper, line: 2, column: 3 },
    attachments: capAttachments,
  });
  const capPlan = publicPlanWithFailure(capFailure);
  assert.equal(withRootOwnedGate(capFixture.gateRoot, () => writeBoundedFailureSummary(
    capFixture.binding,
    { allTests: () => capPlan },
    summarizeRun({ allTests: () => capPlan }, "failed", { publicGate: true }),
    { gateRoot: capFixture.gateRoot, sourceRoot: capFixture.sourceRoot },
  )), true, "an oversized optional image inventory does not discard the failed test identity");
  const capReportBytes = fs.readFileSync(
    path.join(capFixture.results, "live-failures-v2.json"),
  );
  const capReport = JSON.parse(capReportBytes.toString("ascii"));
  assert.ok(capReportBytes.length <= 16 * 1024 - 256);
  assert.equal(capReport.failures.length, 1);
  assert.equal(capReport.failures[0].id, "image_csp_hosts");
  assert.equal(capReport.failures[0].status, "failed");
  assert.equal(capReport.failures[0].image_inventory_state, "captured");
  assert.deepEqual(
    capReport.failures[0].image_inventory.routes.map((route) => [
      route.route,
      route.image_count,
      route.unloaded_count,
    ]),
    [["home", 16, 16], ["patch", 16, 16], ["tournaments", 16, 16]],
    "byte compaction preserves all route aggregates",
  );
  assert.ok(capReport.failures[0].image_inventory.routes.every(
    (route) => route.image_entries_truncated && route.images.length < route.image_count,
  ));

  const invalidImageEvidence = [
    ["unknown private fields", {
      ...imageInventoryV2Payload("home", { images: [imageInventoryV2Image()] }),
      coverage: {
        network_state: "captured", csp_state: "captured", unmatched_csp_count: 0,
        csp_events_truncated: false, private_host: "private.host",
      },
      images: [imageInventoryV2Image({
        url: "https://private.host/private-src",
        host: "private.host",
        src: "private-src",
        srcset: "private-srcset",
        currentSrc: "private-current-src",
        alt: "private-alt",
        unknown_key: "private-value",
      })],
    }],
    ["inconsistent pending response", imageInventoryV2Payload("home", {
      image_count: 1,
      unloaded_count: 1,
      images: [imageInventoryV2Image({
        complete: false, natural_width_positive: false, loading: "lazy",
        request_state: "pending", response_received: false,
        response_status_class: "2xx", request_finished: false,
        request_failed: false, request_failure_class: "none",
        terminal_duration_bucket: "<1s",
      })],
    })],
    ["invalid ambiguous join", imageInventoryV2Payload("home", {
      image_count: 1,
      unloaded_count: 1,
      images: [imageInventoryV2Image({
        complete: false, natural_width_positive: false,
        request_state: "ambiguous", response_received: false,
        response_status_class: "ambiguous", request_finished: null,
        request_failed: null, request_failure_class: "ambiguous",
        terminal_duration_bucket: "ambiguous",
      })],
    })],
    ["precise duration rejected", imageInventoryV2Payload("home", {
      image_count: 1,
      images: [imageInventoryV2Image({ terminal_duration_bucket: "537ms" })],
    })],
    ["unavailable network coverage requires unavailable joins", imageInventoryV2Payload("home", {
      image_count: 1,
      coverage: {
        network_state: "unavailable", csp_state: "captured", unmatched_csp_count: 0,
        csp_events_truncated: false,
      },
      images: [imageInventoryV2Image()],
    })],
    ["unavailable CSP coverage requires unavailable image state", imageInventoryV2Payload("home", {
      image_count: 1,
      coverage: {
        network_state: "captured", csp_state: "unavailable", unmatched_csp_count: 0,
        csp_events_truncated: false,
      },
      images: [imageInventoryV2Image()],
    })],
    ["truncated CSP coverage cannot claim no observation", imageInventoryV2Payload("home", {
      image_count: 1,
      coverage: {
        network_state: "captured", csp_state: "truncated", unmatched_csp_count: 0,
        csp_events_truncated: true,
      },
      images: [imageInventoryV2Image()],
    })],
    ["entry cap exceeded", imageInventoryV2Payload("home", {
      image_count: 512,
      unloaded_count: 1,
      counts_truncated: true,
      image_entries_truncated: true,
      images: Array.from({ length: 17 }, () => null),
    })],
  ];
  for (const [index, [label, payload]] of invalidImageEvidence.entries()) {
    const invalidV2Fixture = diagnosticFixture(failureRoot, `v2i${index}2b3c`);
    const invalidV2Attachment = rawImageInventoryV2Attachment(payload);
    if (label === "entry cap exceeded") {
      assert.ok(invalidV2Attachment.body.length <= 8 * 1024, "the rejected route stays inside the attachment byte cap");
    }
    const invalidV2Failure = failureTest(invalidV2Fixture, {
      title: publicTitles.at(-1),
      project: "live-webkit-mobile",
      errorLocation: { file: invalidV2Fixture.helper, line: 2, column: 3 },
      attachments: [invalidV2Attachment],
    });
    const invalidV2Plan = publicPlanWithFailure(invalidV2Failure);
    assert.equal(withRootOwnedGate(invalidV2Fixture.gateRoot, () => writeBoundedFailureSummary(
      invalidV2Fixture.binding,
      { allTests: () => invalidV2Plan },
      summarizeRun({ allTests: () => invalidV2Plan }, "failed", { publicGate: true }),
      { gateRoot: invalidV2Fixture.gateRoot, sourceRoot: invalidV2Fixture.sourceRoot },
    )), true, `${label} fails closed without dropping the public failure outcome`);
    const invalidV2Bytes = fs.readFileSync(
      path.join(invalidV2Fixture.results, "live-failures-v2.json"),
    );
    const invalidV2Report = JSON.parse(invalidV2Bytes.toString("ascii"));
    assert.deepEqual(
      [invalidV2Report.failures[0].image_inventory_state, invalidV2Report.failures[0].image_inventory],
      ["unavailable", null],
      `${label} is unavailable rather than partially guessed`,
    );
    for (const privateValue of [
      "https://private.host", "private.host", "private-src", "private-srcset",
      "private-current-src", "private-alt", "private-value",
    ]) {
      assert.equal(invalidV2Bytes.includes(Buffer.from(privateValue)), false);
    }
  }

  const validImageForRequestTests = imageInventoryV2Attachment("home", {
    image_count: 1,
    images: [imageInventoryV2Image()],
  });
  const invalidRequestFailurePayloads = [
    ["unknown request evidence key", {
      schema: 1,
      coverage: "since_before_each",
      failure_phase: "hook",
      truncated: false,
      events: [browserRequestFailureEvent({ url: "https://private.host/private-path" })],
    }],
    ["unknown failure phase", {
      schema: 1,
      coverage: "since_before_each",
      failure_phase: "private failure text",
      truncated: false,
      events: [],
    }],
    ["noncontiguous request ordinals", {
      schema: 1,
      coverage: "since_before_each",
      failure_phase: "hook",
      truncated: false,
      events: [browserRequestFailureEvent({ ordinal: 2 })],
    }],
    ["unknown v2 endpoint family", {
      schema: 2,
      coverage: "since_before_each",
      failure_phase: "hook",
      truncated: false,
      events: [browserRequestFailureEventV2({ endpoint_family: "private-path" })],
    }],
  ];
  for (const [index, [label, payload]] of invalidRequestFailurePayloads.entries()) {
    const invalidRequestFixture = diagnosticFixture(failureRoot, `rq${index}2b3c4`);
    const invalidRequestFailure = failureTest(invalidRequestFixture, {
      title: publicTitles.at(-1),
      project: "live-webkit-mobile",
      errorLocation: { file: invalidRequestFixture.helper, line: 2, column: 3 },
      attachments: [validImageForRequestTests, rawBrowserRequestFailureAttachment(payload)],
    });
    const invalidRequestPlan = publicPlanWithFailure(invalidRequestFailure);
    assert.equal(withRootOwnedGate(invalidRequestFixture.gateRoot, () => writeBoundedFailureSummary(
      invalidRequestFixture.binding,
      { allTests: () => invalidRequestPlan },
      summarizeRun({ allTests: () => invalidRequestPlan }, "failed", { publicGate: true }),
      { gateRoot: invalidRequestFixture.gateRoot, sourceRoot: invalidRequestFixture.sourceRoot },
    )), true, `${label} preserves the failed test identity`);
    const invalidRequestBytes = fs.readFileSync(
      path.join(invalidRequestFixture.results, "live-failures-v2.json"),
    );
    const invalidRequestReport = JSON.parse(invalidRequestBytes.toString("ascii"));
    assert.equal(invalidRequestReport.failures[0].image_inventory_state, "captured");
    assert.deepEqual(
      [
        invalidRequestReport.failures[0].browser_request_failure_state,
        invalidRequestReport.failures[0].browser_request_failures,
      ],
      ["unavailable", null],
    );
    for (const privateValue of ["https://private.host", "private.host", "private-path", "private failure text"]) {
      assert.equal(invalidRequestBytes.includes(Buffer.from(privateValue)), false);
    }
  }

  const v2RequestFixture = diagnosticFixture(failureRoot, "rqv2b3c4");
  const v2RequestPayload = {
    schema: 2,
    coverage: "since_before_each",
    failure_phase: "hook",
    truncated: false,
    events: [browserRequestFailureEventV2({
      route_scope: "home",
      failure_route_scope: "patch",
      endpoint_family: "api_patch_content",
      navigation_phase: "pending_route_change",
    })],
  };
  const v2RequestFailure = failureTest(v2RequestFixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    omitSourceLocation: true,
    attachments: [
      validImageForRequestTests,
      rawBrowserRequestFailureAttachment(v2RequestPayload),
    ],
  });
  const v2RequestPlan = publicPlanWithFailure(v2RequestFailure);
  const v2RequestSummary = summarizeRun({ allTests: () => v2RequestPlan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(v2RequestFixture.gateRoot, () => writeBoundedSummary(
    v2RequestFixture.binding,
    v2RequestSummary,
    { gateRoot: v2RequestFixture.gateRoot },
  )), true);
  assert.equal(withRootOwnedGate(v2RequestFixture.gateRoot, () => writeBoundedFailureSummary(
    v2RequestFixture.binding,
    { allTests: () => v2RequestPlan },
    v2RequestSummary,
    { gateRoot: v2RequestFixture.gateRoot, sourceRoot: v2RequestFixture.sourceRoot },
  )), true, "v2 request diagnostics preserve the public failure identity");
  const v2RequestBytes = fs.readFileSync(
    path.join(v2RequestFixture.results, "live-failures-v2.json"),
  );
  const v2RequestReport = JSON.parse(v2RequestBytes.toString("ascii"));
  assert.deepEqual(v2RequestReport.failures[0].browser_request_failures, {
    schema: 2,
    failure_phase: "hook",
    truncated: false,
    events: v2RequestPayload.events,
  });
  const v2RequestGuard = validateEphemeralAndRetainedBrowserReportThroughGuard(
    path.join(v2RequestFixture.results, "live-failures-v2.json"),
  );
  assert.equal(v2RequestGuard.status, 0, v2RequestGuard.stderr);
  for (const privateValue of ["https://", "private-path", "private.host"]) {
    assert.equal(v2RequestBytes.includes(Buffer.from(privateValue)), false);
  }

  const navigationFixture = diagnosticFixture(failureRoot, "nv1b3c4d");
  const navigationTitle = "live CSP nonce is stable on soft navigation and rotates on hard reload";
  const navigationFailure = failureTest(navigationFixture, {
    title: navigationTitle,
    project: "live-desktop",
    omitSourceLocation: true,
    attachments: [navigationFailureAttachment()],
  });
  const navigationPlan = publicPlanWithFailure(navigationFailure);
  const navigationSummary = summarizeRun({ allTests: () => navigationPlan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(navigationFixture.gateRoot, () => writeBoundedSummary(
    navigationFixture.binding,
    navigationSummary,
    { gateRoot: navigationFixture.gateRoot },
  )), true);
  assert.equal(withRootOwnedGate(navigationFixture.gateRoot, () => writeBoundedFailureSummary(
    navigationFixture.binding,
    { allTests: () => navigationPlan },
    navigationSummary,
    { gateRoot: navigationFixture.gateRoot, sourceRoot: navigationFixture.sourceRoot },
  )), true, "closed nonce diagnostics preserve the csp_nonce_rotation identity");
  const navigationBytes = fs.readFileSync(path.join(navigationFixture.results, "live-failures-v2.json"));
  const navigationReport = JSON.parse(navigationBytes.toString("ascii"));
  assert.equal(navigationReport.failures[0].navigation_failure_state, "captured");
  assert.deepEqual(navigationReport.failures[0].navigation_failure, {
    schema: 1,
    test_id: "csp_nonce_rotation",
    result_route: "other",
    query_present: false,
    hash_present: false,
    trailing_slash: false,
    click_dispatched: true,
    click_default_prevented: "unavailable",
    document_response: "not_seen",
    info_rsc_response: "2xx",
    info_rsc_response_phase: "during_click",
    info_rsc_response_timing: "<1s",
    main_frame_commit: "not_seen",
    soft_nonce: "changed",
    reload_nonce: "not_read",
  });
  assert.equal(navigationBytes.includes(Buffer.from("nonce-value")), false);
  const navigationGuard = validateEphemeralAndRetainedBrowserReportThroughGuard(
    path.join(navigationFixture.results, "live-failures-v2.json"),
  );
  assert.equal(navigationGuard.status, 0, navigationGuard.stderr);

  const decodedImageFixture = diagnosticFixture(failureRoot, "im1b3c4d");
  const patchOwnerTitle = "live four current patch DTOs render owner-scoped hero and ability changes";
  const decodedImageFailure = failureTest(decodedImageFixture, {
    title: patchOwnerTitle,
    project: "live-mobile",
    omitSourceLocation: true,
    attachments: [decodedImageFailureAttachment()],
  });
  const decodedImagePlan = publicPlanWithFailure(decodedImageFailure);
  const decodedImageSummary = summarizeRun({ allTests: () => decodedImagePlan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(decodedImageFixture.gateRoot, () => writeBoundedSummary(
    decodedImageFixture.binding,
    decodedImageSummary,
    { gateRoot: decodedImageFixture.gateRoot },
  )), true);
  assert.equal(withRootOwnedGate(decodedImageFixture.gateRoot, () => writeBoundedFailureSummary(
    decodedImageFixture.binding,
    { allTests: () => decodedImagePlan },
    decodedImageSummary,
    { gateRoot: decodedImageFixture.gateRoot, sourceRoot: decodedImageFixture.sourceRoot },
  )), true, "closed decoded-image diagnostics preserve the patch_owner_sections identity");
  const decodedImageBytes = fs.readFileSync(path.join(decodedImageFixture.results, "live-failures-v2.json"));
  const decodedImageReport = JSON.parse(decodedImageBytes.toString("ascii"));
  assert.equal(decodedImageReport.failures[0].decoded_image_failure_state, "captured");
  assert.deepEqual(decodedImageReport.failures[0].decoded_image_failure, {
    schema: 1,
    patch_id: "123456",
    role: "ability",
    section_index: 2,
    element_index: 1,
    state: "decode_rejected",
    decode_error: "rejected",
    current_src_present: true,
    current_src_changed: false,
    width_bucket: "zero",
    height_bucket: "zero",
  });
  for (const privateValue of ["https://", "private asset", "nonce-value"]) {
    assert.equal(decodedImageBytes.includes(Buffer.from(privateValue)), false);
  }
  const decodedImageGuard = validateEphemeralAndRetainedBrowserReportThroughGuard(
    path.join(decodedImageFixture.results, "live-failures-v2.json"),
  );
  assert.equal(decodedImageGuard.status, 0, decodedImageGuard.stderr);

  const screenshotFixture = diagnosticFixture(failureRoot, "scv1b3c4");
  const objectiveScreenshotTitle = "live current patch route and objective source icons remain available";
  const screenshotFailure = failureTest(screenshotFixture, {
    title: objectiveScreenshotTitle,
    project: "live-desktop",
    omitSourceLocation: true,
    attachments: [objectiveScreenshotAttachment()],
  });
  const screenshotPlan = publicPlanWithFailure(screenshotFailure);
  const screenshotSummary = summarizeRun({ allTests: () => screenshotPlan }, "failed", { publicGate: true });
  assert.equal(withRootOwnedGate(screenshotFixture.gateRoot, () => writeBoundedSummary(
    screenshotFixture.binding,
    screenshotSummary,
    { gateRoot: screenshotFixture.gateRoot },
  )), true);
  assert.equal(withRootOwnedGate(screenshotFixture.gateRoot, () => writeBoundedFailureSummary(
    screenshotFixture.binding,
    { allTests: () => screenshotPlan },
    screenshotSummary,
    { gateRoot: screenshotFixture.gateRoot, sourceRoot: screenshotFixture.sourceRoot },
  )), true, "closed screenshot diagnostics preserve the objective_icons failure");
  const screenshotBytes = fs.readFileSync(
    path.join(screenshotFixture.results, "live-failures-v2.json"),
  );
  const screenshotReport = JSON.parse(screenshotBytes.toString("ascii"));
  assert.equal(screenshotReport.failures[0].objective_screenshot_state, "captured");
  assert.equal(screenshotReport.failures[0].objective_screenshot.category, "capture_timeout");
  assert.deepEqual(screenshotReport.failures[0].objective_screenshot.viewport, {
    width: 1430,
    height: 1000,
  });
  const screenshotGuard = validateEphemeralAndRetainedBrowserReportThroughGuard(
    path.join(screenshotFixture.results, "live-failures-v2.json"),
  );
  assert.equal(screenshotGuard.status, 0, screenshotGuard.stderr);
  for (const privateValue of ["https://", "/private", "private message", "cookie=secret"]) {
    assert.equal(screenshotBytes.includes(Buffer.from(privateValue)), false);
  }
  assert.equal(screenshotBytes.length <= 16 * 1024 - 256, true);

  const invalidScreenshotFixture = diagnosticFixture(failureRoot, "siv1b3c4");
  const invalidScreenshotFailure = failureTest(invalidScreenshotFixture, {
    title: objectiveScreenshotTitle,
    project: "live-desktop",
    omitSourceLocation: true,
    attachments: [objectiveScreenshotAttachment({ category: "private-path" })],
  });
  const invalidScreenshotPlan = publicPlanWithFailure(invalidScreenshotFailure);
  const invalidScreenshotSummary = summarizeRun(
    { allTests: () => invalidScreenshotPlan }, "failed", { publicGate: true },
  );
  assert.equal(withRootOwnedGate(invalidScreenshotFixture.gateRoot, () => writeBoundedSummary(
    invalidScreenshotFixture.binding,
    invalidScreenshotSummary,
    { gateRoot: invalidScreenshotFixture.gateRoot },
  )), true);
  assert.equal(withRootOwnedGate(invalidScreenshotFixture.gateRoot, () => writeBoundedFailureSummary(
    invalidScreenshotFixture.binding,
    { allTests: () => invalidScreenshotPlan },
    invalidScreenshotSummary,
    { gateRoot: invalidScreenshotFixture.gateRoot, sourceRoot: invalidScreenshotFixture.sourceRoot },
  )), true, "malformed screenshot evidence preserves the objective_icons failure");
  const invalidScreenshotBytes = fs.readFileSync(
    path.join(invalidScreenshotFixture.results, "live-failures-v2.json"),
  );
  const invalidScreenshotReport = JSON.parse(invalidScreenshotBytes.toString("ascii"));
  assert.deepEqual([
    invalidScreenshotReport.failures[0].objective_screenshot_state,
    invalidScreenshotReport.failures[0].objective_screenshot,
  ], ["unavailable", null]);
  assert.equal(invalidScreenshotBytes.includes(Buffer.from("private-path")), false);
  const invalidScreenshotGuard = validateEphemeralAndRetainedBrowserReportThroughGuard(
    path.join(invalidScreenshotFixture.results, "live-failures-v2.json"),
  );
  assert.equal(invalidScreenshotGuard.status, 0, invalidScreenshotGuard.stderr);

  const oversizedEvidence = imageInventoryV2Payload("home", {
    image_count: 1,
    images: [imageInventoryV2Image()],
  });
  oversizedEvidence.unused = "x".repeat(9 * 1024);
  const oversizedAttachment = rawImageInventoryV2Attachment(oversizedEvidence);
  assert.ok(oversizedAttachment.body.length > 8 * 1024);
  const oversizedFixture = diagnosticFixture(failureRoot, "v2o2b3c4");
  const oversizedFailure = failureTest(oversizedFixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
    errorLocation: { file: oversizedFixture.helper, line: 2, column: 3 },
    attachments: [oversizedAttachment],
  });
  const oversizedPlan = publicPlanWithFailure(oversizedFailure);
  assert.equal(withRootOwnedGate(oversizedFixture.gateRoot, () => writeBoundedFailureSummary(
    oversizedFixture.binding,
    { allTests: () => oversizedPlan },
    summarizeRun({ allTests: () => oversizedPlan }, "failed", { publicGate: true }),
    { gateRoot: oversizedFixture.gateRoot, sourceRoot: oversizedFixture.sourceRoot },
  )), true, "an oversized attachment is rejected while preserving failure identity");
  const oversizedBytes = fs.readFileSync(
    path.join(oversizedFixture.results, "live-failures-v2.json"),
  );
  const oversizedReport = JSON.parse(oversizedBytes.toString("ascii"));
  assert.deepEqual(
    [oversizedReport.failures[0].image_inventory_state, oversizedReport.failures[0].image_inventory],
    ["unavailable", null],
  );
  assert.equal(oversizedBytes.includes(Buffer.from("unused")), false);

  const unavailableFixture = diagnosticFixture(failureRoot, "c2a2b3c4");
  const unavailable = failureTest(unavailableFixture, {
    title: publicTitles.at(-1),
    project: "live-webkit-mobile",
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
  assert.deepEqual(
    [
      unavailableReport.failures[0].image_inventory_state,
      unavailableReport.failures[0].image_inventory,
    ],
    ["unavailable", null],
    "a missing image attachment is unavailable, not a zero-image observation",
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

  const badImageEvidenceCases = [
    ["duplicate route", [
      imageInventoryAttachment("home", {
        image_count: 1, missing_current_src_count: 0, unloaded_count: 0, complete: true,
      }),
      imageInventoryAttachment("home", {
        image_count: 1, missing_current_src_count: 0, unloaded_count: 0, complete: true,
      }),
    ]],
    ["oversize attachment", [{
      name: "live-image-inventory-evidence-v1",
      contentType: "application/json",
      body: Buffer.alloc(513, 0x61),
    }]],
    ["malformed image attachment", [{
      name: "live-image-inventory-evidence-v1",
      contentType: "application/json",
      body: Buffer.from("{", "ascii"),
    }]],
  ];
  for (const [index, [label, attachments]] of badImageEvidenceCases.entries()) {
    const invalidFixture = diagnosticFixture(
      failureRoot,
      `e${index.toString().padStart(2, "0")}2b3c4`,
    );
    const invalid = failureTest(invalidFixture, {
      title: publicTitles.at(-1),
      project: "live-webkit-mobile",
      errorLocation: { file: invalidFixture.helper, line: 2, column: 3 },
      attachments,
    });
    const invalidPlan = publicPlanWithFailure(invalid);
    assert.equal(withRootOwnedGate(invalidFixture.gateRoot, () => writeBoundedFailureSummary(
      invalidFixture.binding,
      { allTests: () => invalidPlan },
      failedSummary,
      { gateRoot: invalidFixture.gateRoot, sourceRoot: invalidFixture.sourceRoot },
    )), true, `${label} does not change the QA failure outcome`);
    const invalidReport = JSON.parse(fs.readFileSync(
      path.join(invalidFixture.results, "live-failures-v2.json"),
      "ascii",
    ));
    assert.deepEqual(
      [
        invalidReport.failures[0].image_inventory_state,
        invalidReport.failures[0].image_inventory,
      ],
      ["unavailable", null],
      `${label} fails closed without inventing image counts`,
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
    file: "tests/support/live-qa-sandbox.ts",
    line: 10_000,
    column: 3,
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
  assert.equal(fullFailureSummary.attempts.interrupted, 57);
  assert.equal(fullFailureSummary.logical.skip, 6);
  assert.equal(withRootOwnedGate(sizeFixture.gateRoot, () => writeBoundedFailureSummary(
    sizeFixture.binding,
    { allTests: () => maxFailedAttempts },
    fullFailureSummary,
    { gateRoot: sizeFixture.gateRoot, sourceRoot: sizeFixture.sourceRoot },
  )), true, "all 57 validated failed attempts survive the bounded report");
  const maximumReportBytes = fs.readFileSync(
    path.join(sizeFixture.results, "live-failures-v2.json"),
  );
  const maximumReport = JSON.parse(maximumReportBytes.toString("ascii"));
  const maximumGuardValidation = validateRetainedBrowserReportThroughGuard(
    path.join(sizeFixture.results, "live-failures-v2.json"),
  );
  assert.equal(
    maximumGuardValidation.status,
    0,
    maximumGuardValidation.stderr,
    "mixed maximum-size report remains valid under the full closed guard schema",
  );
  assert.ok(maximumReportBytes.length <= 16 * 1024);
  assert.ok(maximumReportBytes.length <= 16 * 1024 - 256);
  assert.equal(maximumReport.failures.length, 57);
  assert.ok(maximumReport.failures.every((failure) => failure.status === "interrupted"));
  assert.ok(maximumReport.failures.every((failure) => (
    failure.id === "image_csp_hosts"
    || (
      !Object.hasOwn(failure, "browser_request_failure_state")
      && !Object.hasOwn(failure, "browser_request_failures")
    )
  )), "request evidence fields stay within the image gate's closed schema");
  assert.ok(maximumReport.failures.every((failure) => (
    (failure.source_file === "tests/support/live-qa-sandbox.ts"
      && failure.source_line === 10_000
      && failure.source_column === 3
      && failure.source_kind === "assertion")
    || (failure.source_file === null
      && failure.source_line === null
      && failure.source_column === null
      && failure.source_kind === "unavailable")
  )));
  assert.ok(maximumReport.failures.some((failure) => failure.source_file === null));
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
  const failureIdentities = maximumReport.failures.map((failure) => (
    `${failure.id}:${failure.project}:${failure.attempt}`
  ));
  assert.equal(new Set(failureIdentities).size, 57, "every failed attempt has a unique test/project/attempt identity");
  assert.deepEqual(
    maximumReport.failures
      .filter((failure) => failure.id === "patch_owner_sections")
      .map((failure) => failure.project)
      .sort(),
    ["live-desktop", "live-mobile", "live-webkit-mobile"],
    "the new patch owner scenario survives compaction in all three projects",
  );
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
