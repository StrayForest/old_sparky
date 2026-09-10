# Authenticated HTML transport runbook

- Status: Active operator procedure
- Owner: Platform maintainers

Use this procedure to separate authenticated HTML latency across direct
Next.js, local Nginx and public Cloudflare. It is diagnostic evidence only; the
canonical external load profile remains the authority for production latency,
correctness, origin safety and cleanup.

## Preconditions

Record the deployed source/release, `nginx -v`, `nginx -t`, the diagnostic
profile state and the exact page path. Use a disposable authenticated test
account and a private one-line Cookie-header file with mode `0600`. Never put a
cookie in shell history, command output, an artifact or a report.

The source Nginx policy disables response buffering only in the dynamic HTML
proxy location and emits `X-Accel-Buffering: no`. API and static locations keep
their existing buffering/cache contracts. The JSON access log records upstream
and client encoding, transfer and buffering headers.

## Correlated SSR and auth/API diagnostic

The `web-ssr-diagnostics` profile enables the existing sampled SSR trace and a
separate API log gate. It also selects the public `INFO` log level and global
performance logging flag in the API environment, so fast diagnostic rows are
not hidden by stale startup configuration. For the same sampled requests, Next.js sends
`x-platform-ssr-trace: 1` with explicit diagnostic request and Cloudflare
correlation headers to `GET /api/v1/auth/bootstrap`; the API uses those headers
only for the marked diagnostic hop. The API then emits its existing bounded
`request_perf` record even when the request is faster than the normal slow
request threshold. In the separate timeout-path mode below, only requests
marked by the external runner's bounded diagnostic ID are promoted from the
sampled trace to full SSR/API lifecycle evidence; the load shape remains
unchanged. Applying the profile restarts both `deadlock-api` and
`deadlock-web` with readiness checks because the API gate is read at process
startup; restoring `ready-vote-static-8` returns both services to baseline.
The marker is not accepted as a standalone production switch: the API gate is
disabled in the baseline and the route/method check is mandatory.

The production observer joins these records by `request_id`; for the direct
internal API hop it falls back to the same request's `cf_ray` when the API
server does not preserve the incoming request ID. The report exposes the join
method without serializing either identifier. Read
`correlated_html.timeline[].api_request_perf[]` together with the SSR stages:

- `total_ms`/`request_ms`, `sql_ms`, pool wait and the
  `auth_bootstrap_*_ms` fields measure auth/API work inside FastAPI.
- `auth_bootstrap_fetch`, `root_layout` and `first_body_write_attempt` measure
  the surrounding Node SSR path. Compare their gaps with event-loop p95/p99,
  ELU, process CPU and GC duration to identify scheduling/queueing.

Run this only as one explicitly reviewed `web-ssr-diagnostics` window using the
canonical external authenticated-page profile. It does not change workers,
transport, compression, database pools or capacity limits. If the observer
records a web restart or any unexpected status, treat the window as diagnostic
evidence rather than a clean latency result and follow [Web restart evidence](#web-restart-evidence).
Restore `ready-vote-static-8` after the window so the diagnostic keys and API
logging selectors return to their baseline values
(`PLATFORM_SSR_PERF_LOG_ENABLED=false`,
`PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED=false`, `PLATFORM_LOG_LEVEL=INFO` and
`PLATFORM_PERF_LOG_ENABLED=true`).

## Timeout-path diagnostic window

Use this narrow window when the unchanged
`authenticated-page-load-v1` baseline has client `TimeoutError` results but
no 502/OOM evidence. It is a cause-localization run, not a clean baseline,
performance result or optimization A/B. Keep the load contract unchanged
(20,000 users, 40 tournaments, HTTP concurrency 64, no retries) and do not
change workers, pools, admission, timeout values, auth/cache/security
semantics, Nginx/Cloudflare behavior or application logic.

The external runner assigns one bounded `tdiag-<workflow-run-id>-<user-index>`
ID to each page request and records UTC start, timeout/exception and finish
times. The opt-in header is retained in the Nginx access record. During the
window, Next.js and the internal auth/API request carry that same diagnostic
ID; the API emits a start and completion record, while the web observer joins
SSR stream lifecycle, Nginx upstream status/timings, timestamped event-loop
samples, web CPU/active diagnostic requests and PostgreSQL wait/connection
state. The report keeps Nginx's own request ID separately so the two identities
cannot be confused.

Run sequencing is deliberately four-step:

1. Record the current release, runtime profile and `MemoryMax`; enable the
   existing `web-ssr-diagnostics` operator profile through the production
   deploy workflow. For the reviewed source on `dev`, the operator command is:

   ```bash
   gh workflow run platform-production-deploy.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f mode=deploy -f runtime_profile=web-ssr-diagnostics \
     -f web_compression=enabled
   gh run watch <diagnostic-profile-deploy-run-id> \
     --repo StrayForest/old_sparky --exit-status
   ```

   This only enables temporary bounded logs and restarts the two services with
   readiness checks.
2. Run the external workflow with the dedicated timeout confirmation:

   ```bash
   gh workflow run platform-production-external-load.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f confirmation=RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS \
     -f control_email=<existing-production-account-email> \
     -f profile_id=authenticated-page-load-v1 \
     -f timeout_diagnostics=true
   gh run watch <diagnostic-run-id> --repo StrayForest/old_sparky --exit-status
   ```

3. Read `timeout-diagnostics.json` per diagnostic ID. Missing Nginx evidence
   means only “not observed at origin” (client vs Cloudflare is unresolved),
   not proof that an edge layer was healthy. API start without completion
   means the API accepted the call but completion was not observed. A later
   Nginx completion is explicitly marked when it occurs at or after the
   client timeout. Compare each row with the nearest event-loop/CPU/system
   sample; do not infer CPU saturation from aggregate CPU alone.
4. Restore the exact pre-window runtime profile through the production deploy
   workflow (substitute the recorded profile in the assignment below), for
   example:

   ```bash
   PRE_WINDOW_PROFILE="ready-vote-static-8"
   gh workflow run platform-production-deploy.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f mode=deploy -f "runtime_profile=$PRE_WINDOW_PROFILE" \
     -f web_compression=enabled
   gh run watch <restore-run-id> --repo StrayForest/old_sparky --exit-status
   ```

   Verify both services are ready, confirm diagnostics are off and confirm
   `MemoryMax` is unchanged; this workflow must not be used to tune the
   memory ceiling. The supervisor always performs exact
   fixture cleanup; its temporary manifest and diagnostic-ID handoff are
   removed at the barrier. Do not delete the retained diagnostic artifact
   until the evidence review is complete, then apply the normal bounded
   storage-retention procedure.

Only after the evidence identifies a reversible bottleneck may an operator
propose a separate fix for approval. Do not rerun the full 20,000/20,000
acceptance baseline or the correlated performance run as part of this window.

## Same-request hop probe

Run from an operator host or the origin. The probe reads the cookie, measures
time to response headers and total body time, records selected transport
headers, and never prints the cookie or response body:

```bash
cd /opt/oldsparky/platform/current
python3 tools/platform_ttfb_probe.py \
  --cookie-file /run/oldsparky/qa-cookie \
  --request-id ttfb-hop-<run-id> \
  --hop next=http://127.0.0.1:3000/tournaments/<slug> \
  --hop nginx=https://127.0.0.1/tournaments/<slug> \
  --hop cloudflare=https://old-sparky.com/tournaments/<slug>
```

Compare `ttfb_ms`, `content-encoding`, `transfer-encoding`,
`x-accel-buffering`, `cf-cache-status` and `cf-ray`. The public hop is the
visitor-facing evidence. Repeat the probe enough times to see variance; do not
add its single-request values to external-load p95s.

## Compression A/B

Next compression is a build-time switch, not a runtime profile toggle. The
production deploy workflow exposes `web_compression=enabled|disabled`; it
defaults to `enabled`, passes the choice through the sanitized CI build
environment, and records the resulting choice in `RELEASE.json`. Keep the
default artifact compressed, then deploy a separately named candidate from the
same reviewed source SHA with `web_compression=disabled`:

```bash
gh workflow run platform-production-deploy.yml \
  --ref dev \
  -f mode=deploy \
  -f runtime_profile=ready-vote-static-8 \
  -f web_compression=disabled
```

Run the same control/candidate request shape and hop probe. Retain compression
unless an unchanged-window comparison shows a reproducible first-byte gain
without response-integrity or security regressions. Do not disable Nginx or
Cloudflare compression globally as a first response.

## Cloudflare evidence

Run the read-only Cloudflare audit workflow and inspect
`response-buffering-zone-setting` plus `response-body-buffering-rules`. The
first is the legacy Enterprise zone setting; the second reports per-request
Configuration Rules. Do not change either setting from this repository. A
`none` response-body rule is acceptable only after confirming that the scoped
authenticated HTML path does not require response-body inspection by WAF or
Bot Management.

## Web restart evidence

If an external-load observer records a missing/new `deadlock-web` process, do
not accept the latency result as a clean control or candidate. Dispatch the
read-only `Platform production web runtime diagnostics` workflow from `dev`
with the exact deployed SHA and the observer's UTC window:

```bash
gh workflow run platform-production-web-runtime-diagnostics.yml \
  --ref dev \
  -f expected_sha=<deployed-sha> \
  -f since_utc=2026-09-09T09:33:00Z \
  -f until_utc=2026-09-09T09:54:00Z
```

Inspect systemd exit/result, restart count, memory peak/current, the sanitized
service/kernel journal, and any filesystem/inode/mount facts captured by a
candidate activation failure. Do not raise `MemoryMax`, scale workers or alter
the database pool until the restart cause is identified and a focused rollback
plan exists. A web restart can create both 502s and secondary TTFB queueing.
