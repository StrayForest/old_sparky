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
`x-platform-ssr-trace: 1` with the request and Cloudflare correlation IDs to
`GET /api/v1/auth/bootstrap`. The API then emits its existing bounded
`request_perf` record even when the request is faster than the normal slow
request threshold. Applying the profile restarts both `deadlock-api` and
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

Inspect systemd exit/result, restart count, memory peak and the sanitized
service/kernel journal. Do not raise `MemoryMax`, scale workers or alter the
database pool until the restart cause is identified and a focused rollback
plan exists. A web restart can create both 502s and secondary TTFB queueing.
