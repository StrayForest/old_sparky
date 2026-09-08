# Archived authenticated HTML/TTFB candidate — server-rendered chrome boundaries — 2026-09-08

Status: rejected for production safety; not a target-closing result. Owner:
Platform maintainers.

This candidate followed the safe avatar-defer result and tested whether
reducing the global client boundary surface would lower the remaining Next.js
web CPU/queue cost. It kept the authoritative SSR auth bootstrap, role and
credit projection, no-JavaScript authenticated header, avatar fallback,
pathname-sensitive navigation, Ready Vote, API contracts, worker/pool limits
and PostgreSQL budgets unchanged.

## Candidate and local evidence

The static brand/header/footer markup was moved into Server Components. Small
client islands retained pathname navigation, authenticated actions, retry,
avatar rendering and footer account-link return paths. Local typecheck, lint,
production build, targeted auth/header/footer smoke and the canonical
`web-hermetic` gate passed (479 smoke tests passed, 29 expected skips, and 8
progressive tests passed).

## Production result

Reviewed source SHA
`fe3def967590850390210f05ea567e086899defa` was deployed by the automatic
release chain [`34180793341`](https://github.com/StrayForest/old_sparky/actions/runs/34180793341)
and production deploy [`34180798570`](https://github.com/StrayForest/old_sparky/actions/runs/34180798570).

The unchanged `authenticated-page-load-v1` 20k/40 profile was attempted twice:

- [`34181055402`](https://github.com/StrayForest/old_sparky/actions/runs/34181055402)
  was canceled after 18:43 while the external client remained in the load
  step and produced no acceptance report;
- [`34182385484`](https://github.com/StrayForest/old_sparky/actions/runs/34182385484)
  was independently canceled after 18:34 with the same no-report symptom.

Neither run is a valid latency A/B: no 20k client status/TTFB population was
completed, so no p95 or HTTP-contract claim is made. The repeated failure to
complete the unchanged production load is a production-safety regression and
rejects this candidate regardless of the passing local tests.

Both runs were cleaned through the guarded abort and exact cleanup workflows:

- aborts: [`34182176230`](https://github.com/StrayForest/old_sparky/actions/runs/34182176230)
  and [`34183476498`](https://github.com/StrayForest/old_sparky/actions/runs/34183476498);
- cleanup: [`34182258175`](https://github.com/StrayForest/old_sparky/actions/runs/34182258175)
  and [`34183523985`](https://github.com/StrayForest/old_sparky/actions/runs/34183523985).

Each cleanup reported `ok=true`, one marker, `20,000` users deleted,
`40` tournaments deleted, and zero remaining users, tournaments, sessions or
audit rows. The candidate was then reverted through the reviewed `dev` path;
production is being returned to the last safe SHA
`72e4c5df7af10d3d69c13ca4df938fbdb201e778`.

## Attribution and follow-up

The fresh pre-candidate diagnostic [`34175851102`](https://github.com/StrayForest/old_sparky/actions/runs/34175851102)
had already excluded Nginx connect time, PostgreSQL lock contention and
auth/bootstrap stage time as the primary tail: upstream-header p95 was
`1,119 ms`, sampled root stages were about `80 ms`, web CPU averaged `95.79%`,
and PostgreSQL lock waiters were zero. Its client report was invalid because
of one `IncompleteRead`, so it was not an A/B.

The next concrete candidate is a diagnostic-first stable-contour run after
rollback, followed by one measured reduction in `layout.tsx`/root component
tree work. It must capture completed origin stage timings before any further
production A/B; no worker, pool, admission or timeout change is authorized.
