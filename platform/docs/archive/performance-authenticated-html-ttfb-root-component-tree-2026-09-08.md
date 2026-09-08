# Archived authenticated HTML/TTFB diagnostic — root component tree — 2026-09-08

Status: diagnostic stage complete. No optimization was made. Owner: Platform
maintainers.

## Scope and implementation

The reviewed source SHA
`8b2e1d4537d5eb486d4d58963758c1c2ca8e9f58` was merged by PR #61 and deployed
through the normal exact-SHA production chain. The diagnostic implementation
removed the temporary async client-component boundaries, restored the normal
`Promise.all([headers(), cookies()])` root input path, and added only bounded,
sampled, env-gated timing. API workers and pool/overflow settings, admission,
Ready Vote, authentication semantics, no-JavaScript authenticated header, CSP,
cache policy, thresholds and fixture shape were unchanged.

The diagnostic runtime was enabled only by the reviewed
`web-ssr-diagnostics` profile in deploy
[`34278939241`](https://github.com/StrayForest/old_sparky/actions/runs/34278939241).
The automatic baseline deploy was
[`34276286849`](https://github.com/StrayForest/old_sparky/actions/runs/34276286849).
After measurement, `ready-vote-static-8` was restored by
[`34281338290`](https://github.com/StrayForest/old_sparky/actions/runs/34281338290).

The sampled correlated timeline is:

```text
http_request_start
→ request_to_proxy
→ proxy_start
→ proxy_to_root_layout_start
→ root_layout_start
→ root_layout / auth_bootstrap / page_component
→ react_render_unattributed_start
→ response_stream_start
→ first_body_write_attempt
→ response_finish / response_close / response_error
```

Request IDs and CF-Ray values were used only as sanitized join keys. The Node
request-start epoch, proxy/root offsets and stream elapsed values were
normalized into the root-layout clock. Stream instrumentation recorded
pre-write intent honestly as `first_body_write_attempt`; it also recorded
finish, close, error, writable-finished state, and bounded write/byte totals.

`AuthProvider`, global chrome and `TournamentDetailClientPage` are client
components on this route. They therefore have no fabricated server-render
spans. The route has no custom route layout. Because the workspace is fetched
after hydration, the server did not emit `tournament_detail_data_ready` or
`render_after_data_ready`; browser readiness is not used for server TTFB
attribution.

## Exact production evidence

The unchanged `authenticated-page-load-v1` v1 profile (digest
`32c7d18ada952451d494445485a560045c2bd5d916315d87e7fcf94ad97294e2`) ran as
[`34279322586`](https://github.com/StrayForest/old_sparky/actions/runs/34279322586)
on the exact source SHA above: 20,000 users, 40 tournaments, concurrency 64,
20,000 HTML requests and no retries.

| Population | Requests | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full HTML page | 20,000 | 1,314.085 | 1,639.549 | 1,926.301 | 3,227.415 |
| HTML TTFB | 20,000 | 1,052.722 | 1,325.095 | 1,551.928 | 2,884.688 |

All requests were HTTP 200 (`20,000/20,000`), with zero errors, unexpected
statuses, retries, shedding, incomplete reads or truncated responses. The
client report was complete and the acceptance decision was `STRESS BEHAVIOR
PASS`. The origin observer did not time out. PostgreSQL backends peaked at
`43` against the `52` budget; waiting backends peaked at `1`, lock waiters at
`0`.

The diagnostic population contained 194 sampled requests and 1,940 SSR stage
records. It contained 776 stream events: 194 each for
`response_stream_start`, `first_body_write_attempt`, `response_finish` and
`response_close`; response errors and close-without-finish cases were zero.
All 194 requests had a correlated, clock-aligned timeline. The stream write
totals were bounded at ten writes and roughly 7.2 KiB per sampled response.

The measured server-stage p95s were `root_layout` `64.457 ms`, nested
`auth_bootstrap` `62.773 ms` (including `auth_bootstrap_fetch` `62.538 ms`),
and `page_component` `0.514 ms`. The `root_layout_start` and
`react_render_unattributed_start` entries are points, not component-duration
claims.

The correlated intervals were measured per request, not derived by adding
independent percentiles:

| Interval | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: |
| request start → root layout start | 4.000 | 11.000 | 19.280 | 42.000 |
| proxy start → root layout start | 3.000 | 7.000 | 10.210 | 29.000 |
| render-unattributed start → response stream start | 66.836 | 107.491 | 148.784 | 267.041 |
| render-unattributed start → first body write | 76.407 | 133.869 | 158.864 | 277.041 |
| response stream start → first body write | 9.000 | 18.000 | 35.070 | 37.000 |

The largest measured pre-body segment after the measured server functions is
the unattributed React serialization/flush/response scheduling interval from
`react_render_unattributed_start` to `response_stream_start` (p95
`107.491 ms`). This is the concrete blocking class identified by this stage,
not a claim about a particular client component. No server data-ready marker
exists on this contour, so no post-data component attribution is claimed.
The matching Nginx sampled upstream-header p95 was `1,109.1 ms`; it remains a
separate edge/origin timing population and is not substituted for client TTFB.

## Next candidate

The next separately reviewed A/B is the narrow root response
serialization/flush-boundary candidate in
[`active-authenticated-html-ttfb-root-render-flush-2026-09-09.md`](../../performance/active-authenticated-html-ttfb-root-render-flush-2026-09-09.md).
It must preserve the authoritative auth bootstrap, authenticated header and
no-JavaScript behavior while changing only the root response scheduling
boundary around the already-client detail subtree. It must use the same
20k/40/c64 profile and exact response-integrity/cleanup gates. This archive
does not implement that candidate.

Exact cleanup for the diagnostic run passed: 20,000 users and 40 tournaments
were deleted, with zero remaining users, tournaments, sessions or audit logs.
Production finished on `ready-vote-static-8`. The diagnostic stage is closed;
the `<1,000 ms` HTML TTFB p95 target remains open.
