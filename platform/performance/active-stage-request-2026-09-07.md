# Performance stage request — 2026-09-07

This is the active work order for the follow-up performance stage. The
2026-09-06 production matrix remains historical evidence; it is not rewritten
by this stage.

## Objective

Correct the evidence boundary before another production load gate, close the
transient-failure observability gaps, and measure the current authenticated
HTML path before accepting a narrowly scoped D9 shell change.

The required sequence is:

```text
validate measurement → isolate bottleneck → one bounded change → targeted retest
```

No worker/pool scaling, `max_overflow`, Ready Vote admission rewrite, extra
retries, wait queue, anonymous authenticated shell, or full production matrix
is authorized by this work order.

## Baseline and ownership

- Repository branch: `dev`; baseline checkout for this stage: `73f81596`.
- `origin/dev` matched the baseline checkout before implementation began.
- Production baseline documented in `docs/CURRENT.md`: the latest deployed
  SHA is `73f8159622458cd2e38ad0f9257163ba7c596982`.
- PostgreSQL safety budget remains 52 server backends.
- The 2026-09-06 v1/v2 reports recorded TCP established peaks of 54/53. Those
  historical decisions remain FAIL under their old TCP-based gate, but the
  retained evidence also showed normal `pg_stat_activity` ownership of 51.

The current contract has two separate measurements:

| Measurement | Source | Gate role |
| --- | --- | --- |
| `postgres_backend_connections` | `pg_stat_activity` count in the observer snapshot | Safety gate |
| `postgres_backend_ownership` | `pg_stat_activity` grouped by `application_name`, including `unknown` | Attribution |
| `postgres_tcp_established_connections` | `/proc/net/tcp*` local port 5432 | Socket diagnostic only |

Canonical schema-2 profiles use
`max_postgres_backend_connections`. Historical raw artifacts and archived
reports retain their original TCP terminology for auditability.

## Implemented bounded changes

### Measurement correctness

- The observer now records backend count and normalized backend ownership from
  the same `pg_stat_activity` statement snapshot and reports an explicit
  ownership-total consistency result.
- Acceptance checks compare the backend count with
  `max_postgres_backend_connections`; TCP established sockets remain visible
  in the returned origin evidence but cannot satisfy or fail that backend gate.
- Unit coverage proves TCP 54/backend 51 passes and TCP 51/backend 53 fails,
  and proves ownership totals are attributable, including `unknown`.

### Transient observability

- Nginx JSON access records now retain `upstream_connect_time` and
  `upstream_header_time` alongside existing request/response timings.
- Observer summaries aggregate connect/header/response timing for HTML,
  correlated SSR samples, and safe API route classes.
- The external client retains only bounded `cf_ray`, `cf_error_type`,
  `cf_error_origin`, `retry_after`, status, TTFB, total time and error kind for
  unexpected/5xx samples. It never serializes arbitrary headers or response
  bodies.
- The system sampler adds bounded CPU steal, process start/exit detection with
  timestamps, TCP socket-state pressure, listen overflow/drop counters and
  non-invasive conntrack utilization when procfs exposes it.
- The first fresh authenticated-page workflow attempt
  [`34067801649`](https://github.com/StrayForest/old_sparky/actions/runs/34067801649)
  failed closed before fixture creation: the deployed release was `73f8159`,
  but the old `/root/old_sparky` checkout was stale. Cleanup also ran and
  confirmed there was no retained fixture to remove. The load supervisor,
  cleanup and abort paths now execute from the exact immutable
  `/opt/oldsparky/platform/current` release and verify `RELEASE.json`; the
  deployment state machine shares the retained-load lock so the release cannot
  change during measurement or cleanup.
- The first post-deploy control
  [`34089756423`](https://github.com/StrayForest/old_sparky/actions/runs/34089756423)
  created the full 40-tournament/20,000-user fixture and completed 20,000
  authenticated HTML responses with zero errors, timeout, 520 or 522 samples.
  Total p95 was 3,213.900 ms and HTML TTFB p95 was 2,482.642 ms. The load gate
  still failed because cleanup reached the safe-env wrapper with the shared
  production Python path while that wrapper only accepted the retired fixed
  checkout contour; the fixture must be removed by the corrected cleanup path
  before another production run.

### D9 candidate

Code inspection confirmed that `SiteHeader` uses `avatar_url` as a fallback and
does not require responsive variants. The candidate therefore changes the
request-session avatar projection to select one preferred ready variant URL and
returns no full `avatar_media` descriptor to the initial shell. Full profile
surfaces retain the existing descriptor aggregate.

This candidate is not production-accepted until an unchanged-contract A/B
proves improvement in authenticated HTML TTFB/total latency and no auth/media
semantic regression.

## Verification ledger

| Change | Evidence | Risk | Verification |
| --- | --- | --- | --- |
| Backend/TCP contract split | Prior v1/v2 artifacts and existing `pg_stat_activity` ownership sampler | A stale contract could silently gate on the wrong field | Acceptance/unit tests and canonical profile validation |
| Nginx/connectivity diagnostics | Existing structured access log already has request/upstream timing and correlation fields | Log schema drift or sensitive data leakage | Nginx contract tests and parser tests |
| Bounded external 52x diagnostics | Load client already retained `cf_ray`; Cloudflare fields are explicitly allowlisted | Artifact growth or header leakage | External-load unit tests |
| System correlation counters | Procfs reads are local, bounded and optional | Kernel-specific missing files | Instrumentation tests plus retained observer artifact review |
| Exact release contour for retained loads | First fresh attempt exposed a stale trusted-checkout gate before measurement | A moving release could invalidate evidence or cleanup | Release-contract tests, shared deployment/load lock and exact `RELEASE.json` checks |
| D9 minimal shell projection | Current `SiteHeader`, auth schema and profile read-model code | Missing avatar variant or auth/no-JS regression | Backend/web tests, then same-contract QA/preprod A/B |

## Required next gates

1. Run the focused backend/tool/web verification gates and inspect the exact
   retained failure log if any gate fails.
2. After the release-contour fix is deployed, rerun the fresh
   `authenticated-page-load-v1` control and attribute client TTFB through
   Nginx connect/header/response, Next stages, bootstrap duration, pool wait,
   SQL and response bytes.
3. Compare D9 candidate versus unchanged code under the same workload. Keep
   the target TTFB p95 `<1,000 ms` open unless measured evidence closes it.
4. Run corrected v1/v2 targeted production gates only after local/CI checks.
   Require backend peak `<=52`, explained ownership, no timeout/520/522 and
   separate TCP socket reporting.
5. Recheck Ready Vote SLO and v3/read-path targeted contracts before any full
   13-profile production matrix.

## Explicitly not done

- The stale-checkout rejection in run `34067801649` is resolved. Run
  `34089756423` reached the measurement barrier and passed the HTTP response
  portion, but its cleanup gate failed before fixture deletion; the corrected
  cleanup must run before the result can be accepted.
- No historical raw artifact was rewritten.
- No root cause is assigned to the historical 520/522 episodes or anomaly
  `33991798604` without a correlated recurrence.
- Authenticated TTFB `<1s` is not yet proven.
