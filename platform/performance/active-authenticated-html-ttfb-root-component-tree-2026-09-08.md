# Active authenticated HTML/TTFB work order — root component-tree diagnostics — 2026-09-08

Status: active diagnostic-only stage. Owner: Platform maintainers.

This work order supersedes the rejected component-boundary candidates. It owns
one stable-contour attribution window; it does not authorize a performance
optimization or reopen the completed performance matrix.

## Objective

Measure the authenticated `/tournaments/[slug]` request as one correlated
timeline from proxy entry through the first emitted response chunk. Establish
one real blocking root/server-component stage before selecting a separate,
minimal A/B candidate.

The unchanged acceptance target remains HTML TTFB p95 `<1,000 ms`. This stage
does not claim improvement and does not change that threshold.

## Current contour and scope

The current `dev` source serves the route shell from the server. The
`TournamentDetailClientPage` workspace fetch and interactive detail tree run
after hydration, so their client-side readiness must not be represented as a
server-rendered data-ready span. The diagnostic must expose that boundary and
must not silently attribute browser workspace time to root SSR.

The following are explicitly unchanged:

- API workers, PostgreSQL pool size/overflow, admission and wait policy;
- Ready Vote behavior, limits, retries and authoritative workflow rules;
- authentication/session semantics and the no-JavaScript authenticated header;
- CSP, cookies, cache policy, security boundaries and acceptance thresholds;
- API contracts, database schema, fixture shape and cleanup procedure.

## Diagnostic implementation

Use the existing reviewed `web-ssr-diagnostics` runtime profile. It remains
disabled by the baseline profile and uses the existing bounded
`PLATFORM_SSR_PERF_SAMPLE_RATE` (default `0.01`). No cookies, tokens, HTML,
PII or secrets are logged.

For sampled document requests, `proxy.ts` creates a request-local internal
sample marker and proxy-start timestamp after removing any client-supplied
copy. `server-ssr-observability.ts` carries the same trace through React's
request cache and logs bounded spans with `start_ms`, `end_ms`,
`duration_ms` and `outcome`. The request ID and CF-Ray are safe correlation
labels only.

The root timeline stages are:

```text
proxy_to_root_layout_start
→ root_layout_start
→ auth_bootstrap
→ authenticated_provider
→ global_chrome_header
→ route_layout
→ page_component
→ global_chrome_footer
→ response_stream_start
→ first_chunk_emitted
```

`authenticated_provider`, global chrome and route layout are server boundary
assembly spans. They do not pretend to measure execution inside a client
component. `ssr_stream` records are emitted by the Node response hook without
reading response bodies. Internal server auth requests forward only the
validated `X-Request-ID` and `CF-Ray` labels, allowing their existing
`request_perf` records to correlate with the HTML request. Browser workspace
requests remain a separate post-first-byte request population.
On this contour the server does not emit `tournament_workspace_readiness` or
`render_after_data_ready`; both are intentionally absent rather than inferred
from client hydration or browser API timing.

The observer joins sampled `ssr_perf`, `ssr_stream`, Nginx access records and
matching API `request_perf` records. It exports one sanitized per-request
`correlated_html.timeline` ordered by the measured offsets. Quantiles remain
separate summaries; independent p95 values must not be added as a latency
decomposition.

## Local and CI gates

Before any production action:

- run the focused SSR parser and stream-hook tests;
- run web typecheck/lint/build and web-hermetic tests;
- run the backend/security/build, documentation and verification-contract
  gates required by the reviewed dev path;
- run `git diff --check` and verify the diagnostic profile is off in baseline
  configuration.

No optimization change may be included in this stage.

## Exact production diagnostic run

After review and merge through `dev`, deploy the exact source SHA through the
normal automatic production chain with `ready-vote-static-8` and the reviewed
`web-ssr-diagnostics` profile. Then run the unchanged
`authenticated-page-load-v1` contract:

- 20,000 authenticated users;
- 40 tournaments with the existing fixture shape;
- concurrency 64;
- 20,000 authenticated HTML responses;
- no retries;
- exact cleanup of users, tournaments, sessions and audit logs.

The run is attribution-usable only when all 20,000 responses are HTTP 200,
there are zero unexpected statuses, timeouts, 520s, 522s, retries or
`IncompleteRead`/truncated chunked-response errors, the complete client report
exists, the sampled diagnostic population exists, PostgreSQL remains `<=52`,
and exact cleanup leaves zero fixture remnants while preserving the control
account.

If response integrity fails or the client report is absent, stop attribution:
first investigate the streaming/client measurement path and repair its
reliability. Origin-only timings are not evidence of a TTFB improvement.

## Required output

The archived diagnostic report must contain:

1. the measured root/component boundary tree and its sampled population;
2. correlated per-request timelines showing root start, auth/provider,
   component stages, data boundary where applicable, render-ready marker,
   response stream start and first chunk;
3. one largest blocking stage after the measured data boundary, with evidence
   from the same request timeline and its matching Nginx/API records;
4. one minimal candidate for a later, separately reviewed A/B;
5. an explicit statement that no optimization was made in this stage.

Do not select a candidate from independent p95 sums. If the current contour
cannot produce a server data-ready or render-after-data marker because the
workspace is client-deferred, record that as a diagnostic finding and keep it
out of server TTFB attribution.

## Closeout

Once the exact run and response-integrity checks pass, move this work order to
`platform/docs/archive/`, record the measured conclusion there, and leave
`CURRENT.md` pointing only to the next concrete candidate. Keep the runtime
profile and production contour at baseline/`ready-vote-static-8` after the
diagnostic window unless a separately reviewed release says otherwise.
