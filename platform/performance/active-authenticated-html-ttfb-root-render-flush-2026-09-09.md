# Active authenticated HTML/TTFB candidate — root render/flush boundary — 2026-09-09

Status: proposed A/B; no implementation. Owner: Platform maintainers.

The completed root component-tree diagnostic found no server
`data-ready` marker because the tournament workspace is client-deferred. On
194 correlated production requests, the largest measured pre-body interval
was `react_render_unattributed_start → response_stream_start`, p95
`107.491 ms`, followed by `18.000 ms` p95 from stream start to the first body
write. These are per-request intervals from the diagnostic artifact, not a
sum of independent quantiles and not proof that one client component is the
cause.

## Candidate hypothesis

A minimal A/B that changes only the root response
serialization/flush boundary around the existing client detail subtree may
reduce the post-render pre-first-body interval. The A/B must retain the
server-authoritative auth bootstrap, authenticated header and no-JavaScript
identity, and must not introduce a private fallback that changes those
semantics.

## Fixed constraints

- no API workers, DB pool/overflow, admission or Ready Vote changes;
- no authentication, API contract, cookie, CSP, cache or security changes;
- no fixture, acceptance threshold, retry or cleanup changes;
- baseline production contour remains `ready-vote-static-8`;
- use exact `authenticated-page-load-v1` at 20,000 users / 40 tournaments /
  concurrency 64 with no retries;
- require complete client report, zero response-integrity errors, PostgreSQL
  `<=52`, exact cleanup and a correlated per-request timeline;
- do not use origin-only timing as a TTFB improvement claim.

Before implementation, define one smallest reversible boundary change and its
rollback. Run local/CI web tests and the normal reviewed `dev` path first.
Only a successful exact production run can decide whether this candidate is
worth retaining.
