# Production performance stage — 2026-09-06

Status: in progress. This is the working evidence log for the performance
stage requested in [`active-stage-request-2026-09-06.md`](active-stage-request-2026-09-06.md).
The request file is retained verbatim in the repository so the scope survives
context compression.

## Scope and invariants

- Source under test: `dev`, baseline SHA `8430dc8dff8f68799b2c15b90d20ac0cf301162d`.
- Production load profiles and thresholds are unchanged.
- The production matrix contains the 13 dispatchable profiles only. Lifecycle
  profiles remain prohibited on production.
- The anomalous SLO run is recorded separately and is not used as the canonical
  before value.
- No worker, database pool, Redis pool, or offered-load increase is accepted
  without evidence.

## Canonical baseline

Source artifacts were downloaded from the exact production workflow runs. Each
profile digest below is the `load_contract.profile_digest` recorded by the
runner.

| Profile | Run | Contract digest | Result | Requests | p95 ms | p99 ms | TTFB p95 ms | Useful rate / goodput | Notes |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `ready-vote-slo-v2` | 33991972644 | `c13851df…57543a` | PASS | 601 | 192.028 | 424.893 | 191.913 | 16.606/s | canonical SLO |
| `read-mix-human-v2` | 33980142180 | `b2165554…7caf` | PASS | 500 | 231.759 | 432.223 | 231.624 | 16.578/s | 0 errors |
| `read-mix-stress-v2` | 33980252128 | `c73f65c8…853b` | PASS | 30,000 | 1,697.138 | 2,032.400 | 1,694.728 | 109.349/s | 20k 200 + 10k 304 |
| `read-mix-concurrency-ramp-v1` | 33981247227 | `0b25fd17…06b7c` | FAIL | 170,000 | 1,390.286 | 1,866.793 | 1,388.791 | 90.009/s | 5 c16 timeouts; ceiling ~105–107/s |
| `authenticated-page-load-v1` | 33983691824 | `fe29f8bf…6b57` | PASS (stress) | 20,000 | 3,744.723 | 4,203.714 | 3,158.586 | 28.233/s | 20k HTTP 200 |
| `ready-vote-capacity-ramp-v2` | 33985502827 | `f4956f9f…a533c` | FAIL / incomplete | 10,824 | 237.551 | 578.932 | 237.434 | 49.534/s | user baseline: SLO capacity 70 actions/s; 80 sheds 12.9% |
| `ready-vote-saturation-ramp-v1` | 33986439355 | `804c6c5f…451f` | PASS | 15,225 | 349.883 | 476.105 | 349.802 | 99.118/s | shedding 1.283% |
| `ready-vote-saturation-ramp-v2` | 33987389257 | `47452144…0d54` | PASS | 20,174 | 461.056 | 654.687 | 460.912 | 136.974/s | shedding 16.103% |
| `ready-vote-saturation-ramp-v3` | 33988234852 | `d34c2537…5fd8` | FAIL | 14,798 | 413.948 | 7,787.051 | 402.181 | 94.771/s | 79 timeouts, 1×522 |
| `ready-vote-saturation-ramp-v4` | 33989100146 | `be8a2da8…2e17` | PASS | 16,368 | 391.235 | 498.872 | 391.161 | 124.447/s | final failure 0.386% |
| `ready-vote-stress-15k-v2` | 33989944157 | `a9fb7897…2c8` | PASS (stress) | 26,391 | 547.325 | 699.459 | 547.061 | 132.650/s | shedding 46.475% |
| `ready-vote-stress-20k-v2` | 33990636852 | `04b969a1…d9d` | PASS (stress) | 37,917 | 475.202 | 619.312 | 475.023 | 127.711/s | shedding 52.671%; CPU ~94% |
| `ready-vote-spike-v1` | 33991498696 | `6351a06a…a52c` | PASS | 1,804 | 268.843 | 529.358 | 268.740 | 23.835/s | 1,800/1,800; 0 errors |

### Separate anomaly

Run `33991798604` used the same SLO contract digest as the canonical SLO
profile, but produced 72 `TimeoutError`, 2×520, 28 controlled 503 responses,
and low origin CPU/load. The next SLO run `33991972644` passed after a
read-only health check. Available artifacts do not correlate the failure with
a proved origin, database, Redis, socket, deployment, or Cloudflare cause.
This remains an unexplained transient production anomaly; no external cause is
asserted.

### Targeted-run setup failure

The attempted auth targeted run `34020346471` is not a performance sample. Its
fixture supervisor stopped before creating the barrier because production was
still released at `9f48eadf`, while the workflow target was the later doc-only
SHA `12263cf7`. The exact cleanup guard reported the same active-release SHA
mismatch. The supervisor checks the release before creating its run root or
fixture inventory, and the artifacts contain no manifest, client report, or
ready marker. This is recorded as a workflow setup failure; the auth profile
must be rerun only against a deployed exact SHA.

## Runtime baseline

The reviewed public baseline and deployment/runbook records show:

| Layer | Baseline |
| --- | --- |
| API process | 2 API workers; Uvicorn loop/http `auto` |
| API DB pool | size 24; max overflow 0; pre-ping on; timeout 10s; recycle 1800s |
| Worker DB pool | size 2; max overflow 0; timeout 5s; recycle 1800s; worker concurrency 2 |
| Connection safety | DB connection budget 52 |
| Ready Vote admission | adaptive controller; min/initial/max 4/8/16 per worker; no waiters; zero wait timeout |
| Authenticated reads | reviewed `authenticated-read-admission-32` profile: process-local limit 32, no waiters, zero wait timeout; this is the selected operating protection, not a throughput substitute |
| Nginx | API/web upstream keepalive 32; proxy connect/read/send 5/30/30s; client keepalive 30s; send timeout 30s; gzip enabled |
| Redis | shared async client; read-model caches; no evidence yet of Redis saturation |
| Observability | request perf logs enabled for slow requests; SQL threshold 25; SSR diagnostics disabled in canonical baseline; CPU profiler dormant |

## Evidence before changes

### Authenticated page

Run `33983691824` reports client p95 3,744.723 ms and TTFB p95 3,158.586
ms. Nginx observed 20,000 authenticated HTML responses with request-time p95
3,651.05 ms and upstream-time p95 3,652.00 ms. This puts the dominant wait
inside the web upstream path rather than at the client or Nginx transfer.

The sampled API diagnostics show:

- `/bootstrap`: 15,320 requests, 2 SQL statements/request on average, DB p95
  585.439 ms, pool checkout p95 496.699 ms, non-SQL p95 1,703.716 ms, request
  p95 2,062.230 ms.
- `/workspace`: 8,946 sampled requests, 4.013 SQL/request, DB average
  400.584 ms, pool checkout p95 656.245 ms, Redis workspace read-model p95
  GET 155.403 ms, request p95 1,534.463 ms.
- The measured serialization p95 is only 2.33 ms for workspace samples; it is
  not the 3-second TTFB bottleneck.
- Dedicated SSR diagnostic run `34015185444` used the unchanged source and the
  same `fe29f8bf…6b57` contract. It returned 20,000/20,000 HTTP 200 responses
  with zero errors, but reproduced the slow shape: client TTFB p95 3,237.813
  ms and total p95 3,773.658 ms. Nginx request/upstream p95 was 3,540.15 ms,
  so the delay remains inside the web upstream.
- SSR stage data attributes the internal waits to `auth_bootstrap_fetch`
  (p95 2,006.707 ms) and `tournament_workspace` (p95 1,511.702 ms). The
  sampled Node event-loop lag p95 was 113.872 ms, which does not support a
  primary event-loop CPU stall. The API side of `/bootstrap` still showed DB
  p95 590.530 ms, pool checkout p95 457.410 ms, and non-SQL p95 1,628.930 ms.
- Correlated HTML samples still contain an unattributed upstream-after-data
  component (p95 2,006.390 ms). This is a measurement boundary, not yet a
  proven renderer defect; component/render/stream scheduling must be isolated
  before changing the web path. The run's 25-minute fixture/setup barrier is
  recorded as an operational observation and is not treated as request
  latency.

### Read ceiling

The read stress origin summary reports CPU per core averaging about 98.6%,
while PostgreSQL averages about 25.4% CPU, has no lock waits, and reaches only
about 51 connections. Read-ramp stages are:

| Concurrency | p95 ms | p99 ms | Requests/s |
| ---: | ---: | ---: | ---: |
| 16 | 457.086 | 578.887 | 46.612 |
| 32 | 538.639 | 692.213 | 89.405 |
| 48 | 750.719 | 1,001.839 | 105.936 |
| 64 | 1,095.442 | 1,383.203 | 104.366 |
| 80 | 1,250.656 | 1,496.990 | 106.165 |
| 96 | 1,383.156 | 1,701.923 | 107.421 |
| 112 | 1,611.179 | 1,984.782 | 104.604 |
| 128 | 1,770.142 | 2,094.759 | 105.650 |

The evidence supports an API/Python CPU knee around c48, with useful
throughput flat at approximately 105–107 requests/s. Pool checkout p95 in the
ramp was 225.945 ms and PostgreSQL lock waits were absent; increasing the pool
or concurrency is therefore not a justified first change.

### Saturation v3

The failure is isolated to `rate-105`: 79 client timeouts, one 522, and 80
unexpected outcomes. That phase also had 1,248 controlled 503 responses. The
API observer saw 1,437 completed admission responses with p95 2.890 ms, zero
SQL, and zero pool wait. Thus the early-shed path is cheap and healthy, while
the 80 unexpected requests were admitted requests that did not complete within
the client boundary. The exact timeout path is not yet proven; v1/v2/v3/v4
comparison and correlated Nginx/application access records remain required.

### DB, pool, and cache conclusion

The current measurements do not prove PostgreSQL or Redis as the read ceiling:
PostgreSQL lock waits are zero, CPU is materially below API CPU in read stress,
and Redis read-model hits dominate workspace reads. Pool checkout becomes a
queue under load, but it is downstream of the saturated API and must not be
expanded without an A/B result. No index or SQL rewrite is approved from the
current evidence alone.

## Change ledger

Every optimization must add one row here before targeted retest.

| ID | Problem | Evidence | Change | Expected effect | Risk | Test | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | Auth/read attribution incomplete | Canonical run has no SSR stage or CPU call-stack data | SSR diagnostic and read cProfile on unchanged source | Attribute web upstream and Python CPU time | Temporary diagnostic overhead; no persistent profiler | Same authenticated/read profiles; restore baseline after each | SSR evidence captured in `34015185444`; cProfile evidence captured in `34016833739`; runtime restored by `34018104403` |
| D2 | Read-mix API workers spend CPU scanning the optional-auth cache | cProfile run `34016833739` attributes about 24–25 seconds per worker profile to `_trim_optional_auth_session_cache`, a full dict scan on every cache insert; API CPU was ~99% while PostgreSQL CPU averaged 14.85% and DB connections stayed below the 52-connection safety ceiling | Sweep expired optional-auth entries at most every 5 seconds; retain per-entry expiry/status validation and the existing oldest-entry capacity eviction | Remove repeated O(n) cache scans, reducing Python CPU per authenticated read and potentially raising useful read throughput without changing workers, pools, auth semantics, or load thresholds | Expired untouched entries remain resident until the next sweep (bounded by 5s); cache remains capacity-bounded; invalidate paths are unchanged | New cache unit test; backend/security/quality gates; exact `read-mix-stress-v2` targeted A/B on the same contract; restore normal runtime after diagnostics | Accepted: run `34019227166` on `9f48eadf` returned 20,000×200 + 10,000×304 with 0 errors/unexpected; combined p95 `1697.138→1596.691 ms` (−5.9%), TTFB p95 `1694.728→1596.526 ms` (−5.8%), p99 `2032.400→1930.282 ms` (−5.0%), useful rate `109.349→113.622/s` (+3.9%); CPU remained ~98%/core, so further CPU work is required |
| D3 | v3 and anomaly reports do not expose API-side Nginx status/timing distributions | Existing observer retained only HTML Nginx aggregates; v3 exposed client timeouts but its Nginx API path was not available for correlation | Add bounded API Nginx aggregation by safe route class, method, status, request/upstream timing, and `cf_ray` presence; never serialize URI or request IDs | Identify whether unexpected v3/anomaly outcomes reached Nginx/API and where time was spent, without changing application behavior | Report schema changes; route classes are intentionally coarse; observer still samples the log window | Unit test for route-class/status/timing redaction; local observer/parser gates; deploy exact SHA; rerun v3 and correlate artifacts | Pending |

## Required next sequence

1. Identify the next highest-cost read endpoint/function after D2, using the
   cProfile and route/SQL evidence; keep PostgreSQL pool sizes and workers
   unchanged until a measured resource bottleneck justifies them.
2. Correlate the v3 timeout phase and anomaly with the available access,
   application, system, PostgreSQL, Redis, and deployment records. If records
   still cannot prove a path, document the data limitation and improve only the
   minimum bounded observability needed for a future occurrence.
3. Make one evidence-backed code/config change at a time, run its focused unit,
   integration, browser/security/build and performance gates, then compare.
4. Run targeted gates in the requested order. Only after they pass, repeat the
   exact 13-profile production matrix with the same contracts and thresholds.
5. Perform fixture cleanup and observer/lock checks after every production load;
   finish with documentation archive, clean tree, commit, push to `dev`, and
   post-push equality proof.
