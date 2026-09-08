# Web dependency security maintenance — Next.js 16.3.4 — 2026-09-09

Status: complete. Owner: Platform maintainers.

## Change

The security maintenance PR [#63](https://github.com/StrayForest/old_sparky/pull/63)
updated only the web dependency set required by the current advisories:

- `next` and `@next/eslint-plugin-next` to `16.3.4`;
- the existing `sharp` override to `0.35.4`;
- the transitive `baseline-browser-mapping` lock entry to `2.11.21`.

The lockfile was regenerated with npm `11.16.0`. No audit ignore, direct
`baseline-browser-mapping` dependency, API/DB/admission/Ready Vote change,
authentication change, CSP/cache policy change or performance optimization was
introduced. The selected `16.3.4` release is the follow-up to the patched
`16.3.3` release and restores AVIF image optimization. See the
[Next.js advisory](https://github.com/vercel/next.js/security/advisories/GHSA-p293-qw3h-jr36)
and the
[baseline-browser-mapping advisory](https://github.com/advisories/GHSA-w5vr-8v7q-w6rv).

The merged source SHA is
`d178501aad4c75052d3d75ecb5fa3ba01658946a`. Exact-SHA CI
[`34286848025`](https://github.com/StrayForest/old_sparky/actions/runs/34286848025)
passed backend, migrations, web-quality, Web hermetic, security, Python,
documentation and verification-contract gates. `npm audit --audit-level=high`
reported zero vulnerabilities.

## Production release

The normal exact-SHA chain completed successfully:

- automatic production handoff:
  [`34287305137`](https://github.com/StrayForest/old_sparky/actions/runs/34287305137);
- production deploy and live smoke:
  [`34287311538`](https://github.com/StrayForest/old_sparky/actions/runs/34287311538).

Production is serving the security-maintained source on the unchanged
`ready-vote-static-8` contour. The previous source and release artifacts remain
the rollback target; no database migration or downgrade was part of this
release.

## New performance control

Because a Next.js upgrade can change SSR, serialization and streaming timing,
the old diagnostic run on Next.js 16.2.12 is not an A/B control for later
performance candidates. The first unchanged
`authenticated-page-load-v1` control on Next.js 16.3.4 was
[`34287694375`](https://github.com/StrayForest/old_sparky/actions/runs/34287694375)
with profile digest
`32c7d18ada952451d494445485a560045c2bd5d916315d87e7fcf94ad97294e2`, 20,000
users, 40 tournaments, concurrency 64 and no retries.

| Population | Requests | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full HTML page | 20,000 | 1,383.382 | 2,279.541 | 2,524.429 | 5,714.829 |
| HTML TTFB | 20,000 | 958.219 | 1,725.436 | 2,015.202 | 5,380.984 |

The control returned HTTP 200 for all 20,000 requests, with zero errors,
unexpected statuses, retries, overload responses or response-integrity
failures. Acceptance was `STRESS BEHAVIOR PASS`; the origin observer completed
without timeout. PostgreSQL backends peaked at `41` against the `52` budget,
waiting backends at `1`, lock waiters at `0`, and the web process remained at
two workers.

Exact cleanup passed: 20,000 users and 40 tournaments were deleted, with zero
remaining users, tournaments, sessions or audit logs. This control is the only
performance baseline to use for a future Next.js 16.3.4 A/B. It does not claim
an improvement over the historical 16.2.12 diagnostic evidence.
