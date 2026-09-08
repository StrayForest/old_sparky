# Active authenticated HTML/TTFB candidate — server-rendered chrome boundaries — 2026-09-08

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order owns one bounded Next.js web candidate after the avatar
candidate's fresh origin attribution. It does not reopen the completed
performance matrix or change runtime admission, workers, pools or database
budgets.

## Objective and evidence

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained
`2,568.859 ms` baseline and current exact candidate result `1,512.860 ms` to
`<1,000 ms`, preserving the authorized read path, Ready Vote,
authentication/security semantics, no-JavaScript authenticated header and
exact fixture cleanup.

The fresh diagnostic run
[`34175851102`](https://github.com/StrayForest/old_sparky/actions/runs/34175851102)
captured all `20,000` Nginx HTML records despite an invalid client report. It
measured upstream-header p95 `1,119 ms`, upstream-connect p95 `1 ms`, sampled
auth/bootstrap and root component stages around `80 ms`, web CPU `95.79%`
average, PostgreSQL backends `43`, and zero lock waiters. This makes the
single-process Next.js CPU/queue contour the next owner layer.

## Candidate

Keep the existing `AuthProvider`, authoritative server auth bootstrap, role and
credit projection, `SiteHeader` authenticated actions, profile-avatar
behavior, active navigation, retry behavior and footer account-link behavior.

Split the global chrome into server-rendered static markup with small client
islands:

- `SiteHeader` owns the static brand shell; client islands retain pathname
  navigation and auth-state actions;
- `SiteFooter` owns static brand/navigation/game markup; a client island retains
  pathname-sensitive account links;
- client islands remain server-rendered by Next, so the final no-JavaScript
  HTML still contains the authenticated profile/admin/create controls and
  footer navigation;
- no auth cache, anonymous fallback, API contract, session validity rule,
  Ready Vote path, worker/pool/admission setting or SQL behavior changes.

The candidate is rejected if it changes the rendered authenticated header,
mobile layout, avatar fallback, pathname-sensitive links, auth/session
transitions or no-JavaScript behavior; creates unexpected statuses/timeouts or
520/522 responses; exceeds backend `52`; fails exact cleanup; or does not close
the `<1,000 ms` TTFB p95 target.

## Local and production gates

- web typecheck, ESLint and production build must pass;
- targeted desktop/mobile hermetic smoke must cover no-JavaScript auth states,
  header navigation/actions, avatar, footer and transient auth failures;
- Python dependency gates and the canonical verification registry must run in
  CI; a missing local safe dependency is `LOCAL GATE BLOCKED`;
- exact-SHA security/build, automatic production deployment and the unchanged
  `authenticated-page-load-v1` 20k/40-tournament A/B with exact cleanup are
  required;
- after a target-closing result, run Ready Vote SLO, saturation-v3 and
  read-concurrency-ramp regressions.
