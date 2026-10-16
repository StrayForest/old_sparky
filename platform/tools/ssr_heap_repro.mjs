#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import { mkdir, readFile, readdir, stat } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import path from "node:path";
import { networkInterfaces } from "node:os";
import process from "node:process";
import v8 from "node:v8";

export const SCHEMA = 1;
const APP_SOURCE_COMMIT = "3367e50e347a70ef670a7d1a19091669b6a379e7";
const SESSION_COOKIE_NAME = "__Host-old_sparky_session";
const CSRF_COOKIE_NAME = `${SESSION_COOKIE_NAME}_csrf`;
const APP_ROOT = process.env.SSR_HEAP_APP_ROOT;
const WORK_ROOT = process.env.SSR_HEAP_WORK_ROOT;
const APP_PORT = 3100;
const API_PORT = 3199;
export const MAX_REQUESTS = 20_000;
export const MAX_CONCURRENCY = 64;
const FIXTURE_TOURNAMENTS = 40;
const USERS_PER_TOURNAMENT = 500;
const LOAD_WINDOW_MS = 12 * 60 * 1000;
const HARD_STOP_MS = 16 * 60 * 1000;
const MEMORY_LIMIT_BYTES = 1_073_741_824;
const STOP_RATIO = 0.90;
const SAMPLE_PREFIX = "SSR_HEAP_SAMPLE ";
const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);
const LOAD_REGIME = process.env.SSR_HEAP_REGIME ?? "baseline";
const AUTH_TRANSPORT = process.env.SSR_HEAP_AUTH_TRANSPORT ?? "fetch";
const WEB_WORKERS = process.env.SSR_HEAP_WEB_WORKERS ?? "1";
const SSR_PERF_LOG_ENABLED = process.env.SSR_HEAP_SSR_PERF_LOG_ENABLED ?? "false";
let apiForCleanup = null;
let childForCleanup = null;
let childClosedForCleanup = null;
let diagnosticFailure = "internal";
const diagnosticFatalEnums = Object.create(null);
const controllersForCleanup = new Set();

function assert(condition, code) {
  if (!condition) {
    diagnosticFailure = /^[a-z_]{1,40}$/u.test(code) ? code : "internal";
    throw new Error("diagnostic_assertion");
  }
}

export function classifyFatal(text) {
  const patterns = [
    ["node_heap_oom", /JavaScript heap out of memory|Reached heap limit/i],
    ["node_fatal_oom", /FATAL ERROR:.*(?:heap|memory)/i],
    ["node_assert", /Assertion failed|node::Abort/i],
    ["node_uncaught", /uncaughtException|ERR_UNHANDLED_REJECTION/i],
  ];
  for (const [kind, pattern] of patterns) if (pattern.test(text)) return kind;
  return null;
}

function counterObject() {
  return Object.create(null);
}

function addCount(counts, key, amount = 1) {
  counts[key] = (counts[key] ?? 0) + amount;
}

function sameCounterObject(left, right) {
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index] && left[key] === right[key]);
}

export function fixedFixture(kind, targetBytes, slug = "synthetic-tournament", userId = "synthetic-user", organizerId = userId) {
  const now = "2026-10-08T00:00:00.000Z";
  const syntheticUser = {
    id: userId,
    email: `${userId}@example.invalid`,
    display_name: `Synthetic User ${userId.slice(-5)}`,
    status: "active",
    created_at: now,
    roles: [],
    can_create_public_tournaments: false,
    public_tournament_credits: 2,
    private_tournament_credits: 4,
    avatar_url: null,
    avatar_media: null,
  };
  const value = kind === "bootstrap"
    ? syntheticUser
    : {
        tournament: {
          id: `synthetic-id-${slug.slice(-5)}`,
          slug,
          name: "Synthetic Tournament",
          description: "Synthetic active ready-check fixture.",
          organizer_user_id: organizerId,
          starts_at: now,
          registration_starts_at: now,
          registration_closes_at: now,
          status: "registration_closed",
          visibility: "public",
          allowed_ranks: ["Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist", "Emissary", "Oracle", "Phantom", "Ascendant"],
          format_slug: "solo",
          match_format: "bo3",
          final_format: "bo5",
          created_at: now,
          participant_count: 500,
          max_participants: 500,
          teams_count: 2,
        },
        server_time: now,
        current_user: null,
        current_user_active_commitment: null,
        participants: [],
        participants_total: 500,
        participants_limit: 0,
        participants_offset: 0,
        participants_has_more: true,
        participants_available: true,
        bracket: {
          tournament_id: `synthetic-id-${slug.slice(-5)}`,
          tournament_status: "registration_closed",
          status: "pending",
          revision: 0,
          can_manage: false,
          capabilities: {
            can_manage: false,
            can_schedule_matches: false,
            can_report_matches: false,
          },
          teams: [],
          matches: [],
        },
        ready_check: {
          active_round: {
            id: 1,
            tournament_id: `synthetic-id-${slug.slice(-5)}`,
            status: "active",
            eligible_participant_count: 500,
            ready_count: 0,
            declined_count: 0,
            initiated_by_user_id: organizerId,
            created_at: now,
            closed_at: null,
            current_user_choice: null,
          },
          latest_round: {
            id: 1,
            tournament_id: `synthetic-id-${slug.slice(-5)}`,
            status: "active",
            eligible_participant_count: 500,
            ready_count: 0,
            declined_count: 0,
            initiated_by_user_id: organizerId,
            created_at: now,
            closed_at: null,
            current_user_choice: null,
          },
          state_version: 1,
        },
        auto_assignment: null,
      };
  if (kind === "workspace" && targetBytes !== null) {
    value.tournament.description = "";
    const emptyDescriptionBytes = Buffer.byteLength(JSON.stringify(value));
    const descriptionBytes = targetBytes - emptyDescriptionBytes;
    const sentence = "Synthetic tournament details describe an isolated active ready-check with no live participants or account data. ";
    if (!Number.isInteger(descriptionBytes) || descriptionBytes < 0) throw new Error("fixture_size");
    value.tournament.description = sentence.repeat(Math.ceil(descriptionBytes / sentence.length)).slice(0, descriptionBytes);
  }
  const encoded = Buffer.from(JSON.stringify(value));
  if (targetBytes !== null && encoded.length !== targetBytes) throw new Error("fixture_size");
  return encoded;
}

function scanSentinel(carry, chunk, marker) {
  const combined = carry.length === 0 ? chunk : Buffer.concat([carry, chunk]);
  const found = combined.indexOf(marker) !== -1;
  const retained = Math.min(marker.length - 1, combined.length);
  return {
    found,
    carry: Buffer.from(combined.subarray(combined.length - retained)),
  };
}

function classifyAuthSeedOutcome(regime, matched, missing, delayedBootstraps, delayedResponseClosed) {
  if (![matched, missing, delayedBootstraps, delayedResponseClosed].every(Number.isSafeInteger) || [matched, missing, delayedBootstraps, delayedResponseClosed].some((value) => value < 0)) {
    return "invalid_counts";
  }
  if (["baseline", "workspace_residence_proxy_25pct_2_7s"].includes(regime)) {
    return missing === 0 && matched === MAX_REQUESTS ? "pass" : "auth_seed_missing";
  }
  if (regime === "workspace_500_230") return matched + missing === MAX_REQUESTS ? "observed_counts_only" : "invalid_counts";
  if (regime !== "slow_bootstrap_1pct") return "unknown_regime";
  if (missing === 0) return "slow_delay_preserved_auth";
  if (delayedBootstraps > 0 && missing === delayedBootstraps && delayedResponseClosed === delayedBootstraps && matched + missing === MAX_REQUESTS) {
    return "expected_slow_auth_fallback";
  }
  return "auth_seed_mismatch";
}

function syntheticUserIndex(cookie) {
  const cookies = new Map(cookie.split(";").map((part) => {
    const split = part.trim().indexOf("=");
    return split < 0 ? ["", ""] : [part.trim().slice(0, split), part.trim().slice(split + 1)];
  }));
  const session = cookies.get(SESSION_COOKIE_NAME) ?? "";
  const csrf = cookies.get(CSRF_COOKIE_NAME) ?? "";
  const match = session.match(/^S([0-9]{5})[A-Za-z0-9_-]{58}$/u);
  return match && /^[A-Za-z0-9_-]{43}\.[a-f0-9]{64}$/u.test(csrf) ? Number(match[1]) : -1;
}

function syntheticCookies(index) {
  const sessionToken = `S${String(index).padStart(5, "0")}${"s".repeat(58)}`;
  const csrfToken = `${"c".repeat(43)}.${"a".repeat(64)}`;
  return {
    cookie: `${SESSION_COOKIE_NAME}=${sessionToken}; ${CSRF_COOKIE_NAME}=${csrfToken}`,
    csrfToken,
  };
}

function expectedWorkspace500Count(limit) {
  return Math.floor((limit - 1) / 87) + 1;
}

function workspaceResidenceDelayMs(index) {
  if (index % 4 !== 0) return 0;
  return 2_000 + (Math.floor(index / 4) % 6) * 1_000;
}

function expectedWorkspaceResidenceCounts(limit) {
  const bins = counterObject();
  let total = 0;
  for (let index = 0; index < limit; index += 1) {
    const delayMs = workspaceResidenceDelayMs(index);
    if (delayMs > 0) {
      addCount(bins, String(delayMs));
      total += 1;
    }
  }
  return { total, bins };
}

function requestAuthenticatedPage(index, slug, controller) {
  return new Promise((resolve, reject) => {
    const { cookie, csrfToken } = syntheticCookies(index);
    let settled = false;
    let bytes = 0;
    let authSeeded = false;
    let authSeedCarry = Buffer.alloc(0);
    const marker = Buffer.from(`synthetic-user-${index}@example.invalid`);
    const finish = (value, error = null) => {
      if (settled) return;
      settled = true;
      if (error) reject(error);
      else resolve(value);
    };
    const request = httpRequest({
      host: "127.0.0.1",
      port: APP_PORT,
      method: "GET",
      path: `/tournaments/${slug}`,
      agent: false,
      signal: controller.signal,
      headers: {
        Accept: "text/html",
        "Accept-Encoding": "identity",
        Origin: `http://127.0.0.1:${APP_PORT}`,
        "User-Agent": "old-sparky-external-load/1",
        Cookie: cookie,
        "X-CSRF-Token": csrfToken,
        "X-Platform-QA-Phase": "authenticated_page_load",
        Connection: "close",
      },
    }, (response) => {
      response.on("data", (chunk) => {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        bytes += buffer.length;
        if (bytes > 2 * 1024 * 1024) {
          response.destroy(new Error("response_limit"));
          return;
        }
        if (!authSeeded) {
          const scan = scanSentinel(authSeedCarry, buffer, marker);
          authSeeded = scan.found;
          authSeedCarry = scan.carry;
        }
      });
      response.once("end", () => finish({ status: response.statusCode ?? 0, bytes, authSeeded }));
      response.once("error", (error) => finish(null, error));
      response.once("aborted", () => finish(null, new Error("response_aborted")));
    });
    request.once("error", (error) => finish(null, error));
    request.end();
  });
}

const bootstrapBody = fixedFixture("bootstrap", null);
const workspaceBody = fixedFixture("workspace", 2800);
const workspaceBodies = new Map();

export function contractSelfTest() {
  assert(bootstrapBody.length > 0 && !JSON.parse(bootstrapBody).synthetic_padding, "bootstrap_fixture_shape");
  assert(workspaceBody.length === 2800, "workspace_fixture_size");
  const bootstrap = JSON.parse(bootstrapBody.toString("utf8"));
  const runtimeBootstrap = JSON.parse(fixedFixture("bootstrap", null, "synthetic-tournament", "synthetic-user-19").toString("utf8"));
  const workspace = JSON.parse(fixedFixture("workspace", 2800, "synthetic-00001").toString("utf8"));
  assert(bootstrap.id === "synthetic-user" && bootstrap.status === "active", "bootstrap_shape");
  assert(runtimeBootstrap.id === "synthetic-user-19" && runtimeBootstrap.email.endsWith("@example.invalid"), "bootstrap_runtime_shape");
  const synthetic = syntheticCookies(19);
  assert(syntheticUserIndex(synthetic.cookie) === 19, "synthetic_cookie_shape");
  assert(synthetic.cookie.split(";")[0].split("=")[1].length === 64, "synthetic_session_token_length");
  assert(synthetic.csrfToken.length === 108, "synthetic_csrf_token_length");
  assert(syntheticUserIndex(`deadlock_platform_session=${synthetic.cookie.split("=")[1]}`) === -1, "wrong_cookie_rejected");
  const marker = Buffer.from("synthetic-user-19@example.invalid");
  const firstScan = scanSentinel(Buffer.alloc(0), Buffer.from("<email>synthetic-user-"), marker);
  const secondScan = scanSentinel(firstScan.carry, Buffer.from("19@example.invalid</email>"), marker);
  assert(!firstScan.found && secondScan.found, "auth_seed_chunk_boundary");
  assert(classifyAuthSeedOutcome("baseline", MAX_REQUESTS, 0, 0, 0) === "pass", "baseline_auth_seed");
  assert(classifyAuthSeedOutcome("slow_bootstrap_1pct", MAX_REQUESTS - 200, 200, 200, 200) === "expected_slow_auth_fallback", "slow_auth_seed");
  assert(expectedWorkspace500Count(MAX_REQUESTS) === 230 && expectedWorkspace500Count(100) === 2, "workspace_error_hypothesis_counts");
  assert(classifyAuthSeedOutcome("workspace_500_230", MAX_REQUESTS - 230, 230, 0, 0) === "observed_counts_only", "workspace_error_seed_observation");
  const expectedResidence = expectedWorkspaceResidenceCounts(MAX_REQUESTS);
  assert(expectedResidence.total === 5_000, "workspace_residence_count");
  assert(expectedWorkspaceResidenceCounts(100).total === 25, "workspace_residence_first_hundred");
  assert(JSON.stringify(expectedResidence.bins) === JSON.stringify({ "2000": 834, "3000": 834, "4000": 833, "5000": 833, "6000": 833, "7000": 833 }), "workspace_residence_bins");
  assert(sameCounterObject({ "3000": 1, "2000": 2 }, { "2000": 2, "3000": 1 }), "counter_order_independent");
  assert(workspaceResidenceDelayMs(0) === 2_000 && workspaceResidenceDelayMs(1) === 0, "residence_cohort_selection");
  assert([0, 4, 8, 12, 16, 20, 24].map(workspaceResidenceDelayMs).join(",") === "2000,3000,4000,5000,6000,7000,2000", "residence_delay_cycle");
  assert(classifyAuthSeedOutcome("workspace_residence_proxy_25pct_2_7s", MAX_REQUESTS, 0, 0, 0) === "pass", "residence_auth_seed");
  assert(workspace.tournament.slug === "synthetic-00001", "unique_workspace_slug");
  assert(workspace.tournament.participant_count === 500, "synthetic_population");
  assert(workspace.ready_check.active_round.status === "active", "active_ready_check");
  assert(workspace.current_user === null && workspace.participants_limit === 0, "server_workspace_selection");
  assert(workspace.ready_check.active_round.eligible_participant_count === 500, "ready_check_population");
  assert(workspace.ready_check.active_round.current_user_choice === null, "unvoted_viewer");
  assert(FIXTURE_TOURNAMENTS * USERS_PER_TOURNAMENT === MAX_REQUESTS, "fixture_population");
  assert(classifyFatal("FATAL ERROR: Reached heap limit") === "node_heap_oom", "heap_enum");
  assert(classifyFatal("Assertion failed: synthetic") === "node_assert", "assert_enum");
  assert(classifyFatal("synthetic unknown failure") === null, "unknown_fatal");
  assert(MAX_REQUESTS === 20_000 && MAX_CONCURRENCY === 64, "population_contract");
  assert(STOP_RATIO === 0.90, "bounded_workload_contract");
  assert(["baseline", "slow_bootstrap_1pct", "workspace_500_230", "workspace_residence_proxy_25pct_2_7s"].includes(LOAD_REGIME), "load_regime");
  assert(["fetch", "node"].includes(AUTH_TRANSPORT), "auth_transport");
  return { schema: SCHEMA, contract_checks: 36 };
}

function makeApiServer(stats) {
  const server = createServer(async (request, response) => {
    const remote = request.socket.remoteAddress;
    if (!LOOPBACK.has(remote)) {
      stats.non_loopback = true;
      response.writeHead(403).end();
      return;
    }
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const workspaceMatch = url.pathname.match(/^\/api\/v1\/tournaments\/(synthetic-[0-9]{5})\/workspace$/u);
    const cookie = request.headers.cookie ?? "";
    const userIndex = syntheticUserIndex(cookie);
    const userId = userIndex >= 0 ? `synthetic-user-${userIndex}` : "synthetic-user-invalid";
    const tournamentOrganizerId = userIndex >= 0
      ? `synthetic-user-${Math.floor(userIndex / USERS_PER_TOURNAMENT) * USERS_PER_TOURNAMENT}`
      : "synthetic-user-invalid";
    const isBootstrap = url.pathname === "/api/v1/auth/bootstrap";
    const workspaceQueryMatches = workspaceMatch
      && url.searchParams.get("participants_limit") === "0"
      && url.searchParams.get("participants_offset") === "0"
      && url.searchParams.get("include_current_user") === "false"
      && url.searchParams.get("workspace_view") === "detail";
    let body = null;
    if (isBootstrap) {
      body = fixedFixture("bootstrap", null, "synthetic-tournament", userId);
    } else if (workspaceMatch) {
      body = workspaceBodies.get(workspaceMatch[1]);
      if (!body) {
        body = fixedFixture("workspace", 2800, workspaceMatch[1], userId, tournamentOrganizerId);
        workspaceBodies.set(workspaceMatch[1], body);
      }
    }
    if (!body || !request.url || request.method !== "GET" || userIndex < 0 || userIndex >= MAX_REQUESTS
      || (workspaceMatch && (!workspaceQueryMatches
        || Number(workspaceMatch[1].slice(-5)) !== Math.floor(userIndex / USERS_PER_TOURNAMENT)))) {
      addCount(stats.api_unexpected, "rejected");
      response.writeHead(404).end();
      return;
    }
    const endpoint = isBootstrap ? "bootstrap" : "workspace";
    addCount(stats.api_counts, endpoint);
    if (workspaceMatch) addCount(stats.workspace_slug_counts, workspaceMatch[1]);
    const workspaceInjectedError = endpoint === "workspace"
      && LOAD_REGIME === "workspace_500_230"
      && userIndex % 87 === 0;
    if (workspaceInjectedError) addCount(stats.api_workspace_500_injections, endpoint);
    const residenceDelayMs = endpoint === "workspace" && LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s"
      ? workspaceResidenceDelayMs(userIndex)
      : 0;
    if (residenceDelayMs > 0) {
      addCount(stats.api_workspace_residence_injections, endpoint);
      addCount(stats.api_workspace_residence_delay_ms, String(residenceDelayMs));
    }
    const delayedBootstrap = endpoint === "bootstrap"
      && LOAD_REGIME === "slow_bootstrap_1pct"
      && userIndex >= 0
      && userIndex % 100 === 99;
    if (delayedBootstrap) addCount(stats.api_slow_bootstrap_injections, endpoint);
    let closedBeforeEnd = false;
    response.once("close", () => {
      if (!response.writableEnded) {
        closedBeforeEnd = true;
        if (delayedBootstrap) addCount(stats.api_delayed_response_closed, endpoint);
        if (residenceDelayMs > 0) addCount(stats.api_workspace_residence_client_closed, endpoint);
      }
    });
    // Deterministically approximate the captured request-latency quantiles
    // without request data: 90% median-like, 9% p95-like, 1% p99-like.
    const quantileIndex = userIndex >= 0 ? userIndex % 100 : 0;
    const baselineDelay = endpoint === "bootstrap"
      ? quantileIndex === 99 ? 1750 : quantileIndex >= 90 ? 900 : 350
      : quantileIndex === 99 ? 2100 : quantileIndex >= 90 ? 1100 : 450;
    await delay((delayedBootstrap ? 2200 : baselineDelay) + residenceDelayMs);
    if (closedBeforeEnd || response.destroyed) return;
    if (workspaceInjectedError) {
      response.writeHead(500, { "content-length": "0", "cache-control": "no-store" });
      response.end();
      return;
    }
    stats.api_bytes += body.length;
    response.writeHead(200, {
      "content-type": "application/json; charset=utf-8",
      "content-length": String(body.length),
      "cache-control": "no-store",
    });
    response.end(body, () => {
      if (residenceDelayMs > 0) addCount(stats.api_workspace_residence_completed, endpoint);
    });
  });
  return server;
}

function readCgroupMemory() {
  return Promise.all([
    readFile("/sys/fs/cgroup/memory.max", "utf8"),
    readFile("/sys/fs/cgroup/memory.current", "utf8"),
    readFile("/sys/fs/cgroup/memory.peak", "utf8"),
  ]).then(([limitRaw, currentRaw, memoryPeakRaw]) => ({
    limit: Number(limitRaw.trim()),
    current: Number(currentRaw.trim()),
    peak: Number(memoryPeakRaw.trim()),
  }));
}

async function readCgroupCpuLimit() {
  const [quotaRaw, periodRaw] = (await readFile("/sys/fs/cgroup/cpu.max", "utf8")).trim().split(/\s+/u);
  const quota = Number(quotaRaw);
  const period = Number(periodRaw);
  return { quota, period, cores: quota / period };
}

class Aggregates {
  constructor() {
    this.n = 0;
    this.max = Object.create(null);
    this.sum = Object.create(null);
    this.min = Object.create(null);
    this.points = [];
  }
  add(sample) {
    this.n += 1;
    if (this.points.length < 1100) this.points.push({ t_ms: sample.t_ms, heap_used: sample.heap_used });
    for (const key of ["rss", "heap_used", "heap_total", "external", "array_buffers", "heap_limit", "gc_count", "gc_duration_ms", "gc_max_duration_ms", "event_loop_p95_ms", "event_loop_max_ms"]) {
      const value = sample[key];
      if (!Number.isFinite(value) || value < 0) continue;
      this.max[key] = Math.max(this.max[key] ?? 0, value);
      this.min[key] = Math.min(this.min[key] ?? value, value);
      this.sum[key] = (this.sum[key] ?? 0) + value;
    }
  }
  toJSON() {
    const average = Object.create(null);
    for (const [key, value] of Object.entries(this.sum)) average[key] = Math.round(value / Math.max(1, this.n));
    const initial = this.points.slice(0, 60).map((point) => point.heap_used).filter(Number.isFinite);
    const idle = this.points.slice(-60).map((point) => point.heap_used).filter(Number.isFinite);
    const median = (values) => {
      if (values.length === 0) return null;
      const sorted = [...values].sort((left, right) => left - right);
      return Math.round(sorted[Math.floor(sorted.length / 2)]);
    };
    const firstMedian = median(initial);
    const idleMedian = median(idle);
    const start = this.points.at(-60);
    const end = this.points.at(-1);
    const idleSlope = start && end && end.t_ms > start.t_ms
      ? Math.round(((end.heap_used - start.heap_used) / (end.t_ms - start.t_ms)) * 60_000)
      : null;
    return {
      samples: this.n,
      min: this.min,
      max: this.max,
      average,
      first_minute_heap_median_bytes: firstMedian,
      final_minute_heap_median_bytes: idleMedian,
      retained_heap_growth_bytes: firstMedian === null || idleMedian === null ? null : idleMedian - firstMedian,
      final_minute_heap_slope_bytes_per_minute: idleSlope,
      sampled_numeric_points_retained_in_memory: this.points.length,
    };
  }
}

async function main() {
  assert(typeof APP_ROOT === "string" && path.isAbsolute(APP_ROOT), "app_root");
  assert(typeof WORK_ROOT === "string" && path.isAbsolute(WORK_ROOT), "work_root");
  assert(process.env.SSR_HEAP_APP_SOURCE_COMMIT === APP_SOURCE_COMMIT, "app_source_commit");
  assert(WEB_WORKERS === "1", "web_worker_count");
  assert(["true", "false"].includes(SSR_PERF_LOG_ENABLED), "ssr_perf_log_enabled");
  assert(["baseline", "slow_bootstrap_1pct", "workspace_500_230", "workspace_residence_proxy_25pct_2_7s"].includes(LOAD_REGIME), "load_regime");
  assert(["fetch", "node"].includes(AUTH_TRANSPORT), "auth_transport");
  const cap = await readCgroupMemory();
  assert(cap.limit === MEMORY_LIMIT_BYTES, "memory_cap");
  const cpuLimit = await readCgroupCpuLimit();
  assert(cpuLimit.quota === 200_000 && cpuLimit.period === 100_000, "cpu_cap");
  assert(process.version === "v26.3.1", "node_version");
  const interfaces = networkInterfaces();
  assert(Object.keys(interfaces).length === 1 && Array.isArray(interfaces.lo)
    && interfaces.lo.every((address) => address.internal), "network_interfaces");
  await mkdir("/tmp/ssr-heap-home", { recursive: true });
  const serverFile = path.join(APP_ROOT, "server.js");
  await stat(serverFile);
  const shutdownGuardFile = path.join(APP_ROOT, "server-shutdown-guard.cjs");
  await stat(shutdownGuardFile);
  const environment = {
    PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
    HOME: "/tmp/ssr-heap-home",
    NODE_ENV: "production",
    HOSTNAME: "127.0.0.1",
    PORT: String(APP_PORT),
    PLATFORM_API_BASE_URL: `http://127.0.0.1:${API_PORT}/api/v1`,
    PLATFORM_API_INTERNAL_ORIGIN: `http://127.0.0.1:${API_PORT}`,
    NEXT_PUBLIC_PLATFORM_API_BASE_URL: `http://127.0.0.1:${API_PORT}/api/v1`,
    PLATFORM_SESSION_COOKIE_NAME: SESSION_COOKIE_NAME,
    PLATFORM_WEB_WORKERS: WEB_WORKERS,
    PLATFORM_SSR_PERF_LOG_ENABLED: SSR_PERF_LOG_ENABLED,
    PLATFORM_SSR_PERF_SAMPLE_RATE: "0.01",
    PLATFORM_ADSENSE_ENABLED: "false",
    SSR_HEAP_DIAGNOSTIC: "1",
    ...(AUTH_TRANSPORT === "node" ? { PLATFORM_WEB_SERVER_AUTH_TRANSPORT: "node" } : {}),
  };
  const stats = {
    api_counts: counterObject(),
    api_unexpected: counterObject(),
    api_slow_bootstrap_injections: counterObject(),
    api_workspace_500_injections: counterObject(),
    api_delayed_response_closed: counterObject(),
    api_workspace_residence_injections: counterObject(),
    api_workspace_residence_completed: counterObject(),
    api_workspace_residence_client_closed: counterObject(),
    api_workspace_residence_delay_ms: counterObject(),
    workspace_slug_counts: counterObject(),
    api_bytes: 0,
    non_loopback: false,
  };
  const api = makeApiServer(stats);
  apiForCleanup = api;
  await new Promise((resolve, reject) => {
    api.once("error", reject);
    api.listen(API_PORT, "127.0.0.1", resolve);
  });
  assert(LOOPBACK.has(api.address().address), "api_bind");

  const metric = new Aggregates();
  const fatalEnums = diagnosticFatalEnums;
  const fatalSeen = new Set();
  const recordFatal = (kind) => {
    if (kind && !fatalSeen.has(kind)) {
      fatalSeen.add(kind);
      addCount(fatalEnums, kind);
    }
  };
  let stderrTail = "";
  let stderrLine = "";
  let oversizedTelemetry = false;
  let latestHeapUsed = 0;
  let latestHeapLimit = 0;
  const child = spawn(process.execPath, [
    `--require=${shutdownGuardFile}`,
    `--require=${path.join(WORK_ROOT, "ssr_heap_repro_preload.cjs")}`,
    serverFile,
  ], {
    cwd: APP_ROOT,
    env: environment,
    stdio: ["ignore", "ignore", "pipe"],
  });
  childForCleanup = child;
  const childClosed = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
  childClosedForCleanup = childClosed;
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderrTail = (stderrTail + chunk).slice(-4096);
    for (const char of chunk) {
      if (char === "\n") {
        if (stderrLine.startsWith(SAMPLE_PREFIX)) {
          const raw = stderrLine.slice(SAMPLE_PREFIX.length);
          if (raw.length <= 2048) {
            try {
              const parsed = JSON.parse(raw);
              if (parsed.schema === 1) {
                metric.add(parsed);
                latestHeapUsed = parsed.heap_used;
                latestHeapLimit = parsed.heap_limit;
              }
            } catch { /* discard malformed private telemetry */ }
          } else oversizedTelemetry = true;
        }
        stderrLine = "";
      } else if (stderrLine.length < 4096) {
        stderrLine += char;
      } else {
        oversizedTelemetry = true;
        stderrLine = "";
      }
    }
    recordFatal(classifyFatal(stderrTail));
  });

  let launchAt = Date.now();
  let serverReady = false;
  let thresholdStop = false;
  let clientAbort = false;
  let serverDied = false;
  let loadStarted = false;
  let loadFinished = false;
  let stoppingServer = false;
  const statuses = counterObject();
  const first100Statuses = counterObject();
  const authSeedCounts = counterObject();
  const first100AuthSeedCounts = counterObject();
  let responseBytes = 0;
  let started = 0;
  let completed = 0;
  let inflight = 0;
  let maxInflightObserved = 0;
  const active = new Set();
  const controllers = new Set();
  child.once("close", () => {
    if (loadStarted && !stoppingServer) {
      serverDied = true;
      clientAbort = true;
      for (const controller of controllers) controller.abort();
    }
  });

  const deadline = launchAt + HARD_STOP_MS;
  async function probeReady() {
    while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
      try {
        const response = await fetch(`http://127.0.0.1:${APP_PORT}/`, { signal: AbortSignal.timeout(500) });
        await response.body?.cancel();
        if (response.status < 500) return true;
      } catch { /* fixed loopback readiness retry */ }
      await delay(250);
    }
    return false;
  }
  serverReady = await probeReady();
  if (!serverReady) {
    const kind = child.signalCode === "SIGABRT" ? "sigabrt" : child.exitCode !== null ? "exit" : "startup_timeout";
    addCount(fatalEnums, kind);
    diagnosticFailure = "server_start";
    throw new Error("server_start");
  }
  launchAt = Date.now();

  const stopAt = launchAt + LOAD_WINDOW_MS;
  loadStarted = true;
  const hardTimer = setTimeout(() => {
    clientAbort = true;
  }, Math.max(1, deadline - Date.now()));
  hardTimer.unref();

  async function sendOne(index) {
    const tournamentIndex = Math.floor(index / USERS_PER_TOURNAMENT);
    const slug = `synthetic-${String(tournamentIndex).padStart(5, "0")}`;
    const controller = new AbortController();
    controllers.add(controller);
    controllersForCleanup.add(controller);
    const timeout = setTimeout(() => controller.abort(), 30_000);
    try {
      const response = await requestAuthenticatedPage(index, slug, controller);
      addCount(statuses, String(response.status));
      if (index < 100) addCount(first100Statuses, String(response.status));
      responseBytes += response.bytes;
      addCount(authSeedCounts, response.authSeeded ? "matched" : "missing");
      if (index < 100) addCount(first100AuthSeedCounts, response.authSeeded ? "matched" : "missing");
    } catch (error) {
      if (error instanceof Error && error.message === "response_limit") {
        clientAbort = true;
        for (const pending of controllers) pending.abort();
        addCount(statuses, "oversized_response");
        if (index < 100) addCount(first100Statuses, "oversized_response");
      } else {
        addCount(statuses, "transport_error");
        if (index < 100) addCount(first100Statuses, "transport_error");
      }
    } finally {
      clearTimeout(timeout);
      completed += 1;
      inflight -= 1;
      controllers.delete(controller);
      controllersForCleanup.delete(controller);
    }
  }

  // Match the authored zero-spread workload: fill the 64-request window and
  // immediately refill each slot as its prior request completes. No request
  // pacing is imposed; achieved throughput is reported from the measured run.
  const pressureMonitor = setInterval(async () => {
    try {
      const current = await readCgroupMemory();
      if (current.current >= MEMORY_LIMIT_BYTES * STOP_RATIO
        || (latestHeapLimit > 0 && latestHeapUsed >= latestHeapLimit * STOP_RATIO)) {
        thresholdStop = true;
        for (const controller of controllers) controller.abort();
        if (loadFinished && child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      }
    } catch {
      thresholdStop = true;
      for (const controller of controllers) controller.abort();
      if (loadFinished && child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    }
  }, 250);
  pressureMonitor.unref();

  let nextIndex = 0;
  while (nextIndex < MAX_REQUESTS && Date.now() < stopAt && !clientAbort && !thresholdStop && !serverDied) {
    if (nextIndex % MAX_CONCURRENCY === 0) {
      const cgroup = await readCgroupMemory();
      if (cgroup.current >= MEMORY_LIMIT_BYTES * STOP_RATIO || process.memoryUsage().heapUsed >= v8.getHeapStatistics().heap_size_limit * STOP_RATIO) {
        thresholdStop = true;
        break;
      }
    }
    while (inflight >= MAX_CONCURRENCY) await Promise.race(active);
    if (Date.now() - launchAt > HARD_STOP_MS) {
      clientAbort = true;
      break;
    }
    let task;
    task = sendOne(nextIndex).finally(() => active.delete(task));
    active.add(task);
    inflight += 1;
    maxInflightObserved = Math.max(maxInflightObserved, inflight);
    started += 1;
    nextIndex += 1;
  }
  await Promise.allSettled(active);
  loadFinished = true;
  const loadEndAt = Date.now();
  clearTimeout(hardTimer);
  await new Promise((resolve) => api.close(resolve));
  const idleStart = Date.now();
  const idleComplete = !thresholdStop && !serverDied && child.exitCode === null && child.signalCode === null;
  if (idleComplete) await delay(60_000);
  const idleSeconds = Math.floor((Date.now() - idleStart) / 1000);
  clearInterval(pressureMonitor);
  stoppingServer = true;
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  const exited = await Promise.race([childClosed, delay(5_000).then(() => null)]);
  if (!exited && child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  const exit = exited ?? await childClosed;
  const memoryFinal = await readCgroupMemory();
  recordFatal(classifyFatal(stderrTail));
  const expectedWorkspace500Total = expectedWorkspace500Count(MAX_REQUESTS);
  const expectedResidence = expectedWorkspaceResidenceCounts(MAX_REQUESTS);
  const workspaceStatusKeys = Object.keys(statuses);
  const first100WorkspaceStatusKeys = Object.keys(first100Statuses);
  const expectedWorkspaceError = LOAD_REGIME === "workspace_500_230"
    && (statuses["200"] ?? 0) + (statuses["500"] ?? 0) === MAX_REQUESTS
    && workspaceStatusKeys.every((key) => ["200", "500"].includes(key))
    && (first100Statuses["200"] ?? 0) + (first100Statuses["500"] ?? 0) === 100
    && first100WorkspaceStatusKeys.every((key) => ["200", "500"].includes(key))
    && stats.api_workspace_500_injections.workspace === expectedWorkspace500Total
    && stats.api_counts.bootstrap === MAX_REQUESTS
    && stats.api_counts.workspace === MAX_REQUESTS
    && Object.keys(stats.api_unexpected).length === 0
    && !stats.non_loopback
    && !oversizedTelemetry;
  const expectedWorkspaceResidence = LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s"
    && statuses["200"] === MAX_REQUESTS
    && Object.keys(statuses).length === 1
    && first100Statuses["200"] === 100
    && Object.keys(first100Statuses).length === 1
    && authSeedCounts.matched === MAX_REQUESTS
    && !authSeedCounts.missing
    && stats.api_workspace_residence_injections.workspace === expectedResidence.total
    && stats.api_workspace_residence_completed.workspace === expectedResidence.total
    && !stats.api_workspace_residence_client_closed.workspace
    && sameCounterObject(stats.api_workspace_residence_delay_ms, expectedResidence.bins)
    && stats.api_counts.bootstrap === MAX_REQUESTS
    && stats.api_counts.workspace === MAX_REQUESTS
    && Object.keys(stats.api_unexpected).length === 0
    && !stats.non_loopback
    && !oversizedTelemetry;
  const result = serverDied
    ? "server_crash"
    : clientAbort
    ? "bounded_timeout"
    : thresholdStop
      ? "memory_threshold"
      : completed !== MAX_REQUESTS
        ? "incomplete_load"
        : started !== MAX_REQUESTS || maxInflightObserved > MAX_CONCURRENCY
          ? "workload_shape"
        : Object.keys(stats.workspace_slug_counts).length !== FIXTURE_TOURNAMENTS
          ? "workspace_population"
        : LOAD_REGIME === "workspace_500_230"
          ? expectedWorkspaceError ? "workspace_500_hypothesis_complete" : "workspace_500_hypothesis_mismatch"
        : LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s"
          ? expectedWorkspaceResidence ? "workspace_residence_hypothesis_complete" : "workspace_residence_hypothesis_mismatch"
        : statuses["200"] !== MAX_REQUESTS
          ? "http_status"
        : first100Statuses["200"] !== 100
          ? "first_100_http_status"
        : classifyAuthSeedOutcome(
          LOAD_REGIME,
          authSeedCounts.matched ?? 0,
          authSeedCounts.missing ?? 0,
          stats.api_slow_bootstrap_injections.bootstrap ?? 0,
          stats.api_delayed_response_closed.bootstrap ?? 0,
        ) !== "pass"
          ? classifyAuthSeedOutcome(
            LOAD_REGIME,
            authSeedCounts.matched ?? 0,
            authSeedCounts.missing ?? 0,
            stats.api_slow_bootstrap_injections.bootstrap ?? 0,
            stats.api_delayed_response_closed.bootstrap ?? 0,
          )
        : stats.api_counts.bootstrap !== MAX_REQUESTS || stats.api_counts.workspace !== MAX_REQUESTS
            ? "fake_api_count"
            : Object.keys(stats.api_unexpected).length > 0
              ? "unexpected_api"
              : stats.non_loopback
                ? "network_isolation"
                : oversizedTelemetry
                  ? "telemetry_bounds"
                  : "pass";
  const output = {
    schema: SCHEMA,
    result,
    node_version: process.version,
    next_version: "16.3.8",
    react_version: "19.2.7",
    load_regime: LOAD_REGIME,
    auth_transport: AUTH_TRANSPORT,
    client_request_transport: "node_http1_close",
    client_request_timeout_ms: 30_000,
    client_response_limit_bytes: 2 * 1024 * 1024,
    client_origin_is_loopback_substitution: true,
    synthetic_session_token_length: 64,
    synthetic_csrf_token_length: 108,
    web_workers: Number(WEB_WORKERS),
    ssr_perf_log_enabled: SSR_PERF_LOG_ENABLED === "true",
    shutdown_guard_loaded: true,
    source_kind: "exact_h_synthetic_only",
    app_source_commit: APP_SOURCE_COMMIT,
    memory_limit_bytes: cap.limit,
    cpu_quota_cores: cpuLimit.cores,
    network_interfaces_loopback_only: true,
    memory_peak_bytes: memoryFinal.peak,
    memory_final_bytes: memoryFinal.current,
    load_window_ms: loadEndAt - launchAt,
    achieved_requests_per_second: loadEndAt > launchAt ? completed * 1000 / (loadEndAt - launchAt) : 0,
    requested: MAX_REQUESTS,
    fixture_tournaments: FIXTURE_TOURNAMENTS,
    users_per_tournament: USERS_PER_TOURNAMENT,
    distinct_workspace_slugs: Object.keys(stats.workspace_slug_counts).length,
    started,
    completed,
    max_concurrency: MAX_CONCURRENCY,
    max_inflight_observed: maxInflightObserved,
    status_counts: statuses,
    first_100_status_counts: first100Statuses,
    auth_seed_counts: authSeedCounts,
    first_100_auth_seed_counts: first100AuthSeedCounts,
    auth_seed_outcome: classifyAuthSeedOutcome(
      LOAD_REGIME,
      authSeedCounts.matched ?? 0,
      authSeedCounts.missing ?? 0,
      stats.api_slow_bootstrap_injections.bootstrap ?? 0,
      stats.api_delayed_response_closed.bootstrap ?? 0,
    ),
    response_bytes: responseBytes,
    fake_api_counts: stats.api_counts,
    fake_api_slow_bootstrap_injections: stats.api_slow_bootstrap_injections,
    fake_api_workspace_500_injections: stats.api_workspace_500_injections,
    fake_api_workspace_500_expected_count: LOAD_REGIME === "workspace_500_230" ? expectedWorkspace500Total : 0,
    workspace_residence_proxy: LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s",
    workspace_residence_proxy_scope: LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s" ? "workspace_api_wait_only" : null,
    workspace_residence_proxy_delay_min_ms: LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s" ? 2_000 : 0,
    workspace_residence_proxy_delay_max_ms: LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s" ? 7_000 : 0,
    workspace_residence_proxy_expected_count: LOAD_REGIME === "workspace_residence_proxy_25pct_2_7s" ? expectedResidence.total : 0,
    fake_api_workspace_residence_injections: stats.api_workspace_residence_injections,
    fake_api_workspace_residence_completed: stats.api_workspace_residence_completed,
    fake_api_workspace_residence_client_closed: stats.api_workspace_residence_client_closed,
    fake_api_workspace_residence_delay_ms: stats.api_workspace_residence_delay_ms,
    page_http_200_count: statuses["200"] ?? 0,
    page_http_500_count: statuses["500"] ?? 0,
    workspace_http_status_presentation_is_observed: LOAD_REGIME === "workspace_500_230",
    fake_api_delayed_response_closed: stats.api_delayed_response_closed,
    fake_api_bytes: stats.api_bytes,
    telemetry: metric.toJSON(),
    idle_observation_seconds: idleSeconds,
    idle_observation_complete: idleComplete && idleSeconds >= 60,
    threshold_stop: thresholdStop,
    bounded_timeout: clientAbort,
    server_exit: { code: exit.code, signal: exit.signal },
    fatal_enums: fatalEnums,
  };
  process.stdout.write(JSON.stringify(output) + "\n");
  if (result !== "pass" && result !== "workspace_500_hypothesis_complete"
    && result !== "workspace_residence_hypothesis_complete") process.exitCode = 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname)) {
  if (process.argv.includes("--self-test")) {
    try {
      process.stdout.write(JSON.stringify(contractSelfTest()) + "\n");
    } catch {
      process.stdout.write(JSON.stringify({ schema: SCHEMA, result: "contract_test_failed" }) + "\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(async () => {
  for (const controller of controllersForCleanup) controller.abort();
  if (apiForCleanup?.listening) {
    await Promise.race([
      new Promise((resolve) => apiForCleanup.close(resolve)),
      delay(1000),
    ]);
  }
  if (childForCleanup && childForCleanup.exitCode === null && childForCleanup.signalCode === null) {
    childForCleanup.kill("SIGTERM");
    await Promise.race([childClosedForCleanup ?? delay(1000), delay(5000)]);
    if (childForCleanup.exitCode === null && childForCleanup.signalCode === null) childForCleanup.kill("SIGKILL");
  }
  // The public output is a fixed enum; never print exception details, paths,
  // request data, API bodies, environment values, or raw child stderr.
  process.stdout.write(JSON.stringify({
    schema: SCHEMA,
    result: "repro_runner_error",
    failure_code: diagnosticFailure,
    fatal_enums: diagnosticFatalEnums,
  }) + "\n");
  process.exitCode = 1;
    });
  }
}
