# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-09-11

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Verified checkpoint — 2026-09-11

- Production is on the standard `ready-vote-static-8` runtime with compression
  enabled, the normal `fetch` auth transport and Node `26.3.1`. The latest
  behavior-bearing restore is
  [`34550534777`](https://github.com/StrayForest/old_sparky/actions/runs/34550534777)
  at source SHA `0700b7402ecdd182fe0cfba4feae14f15fb68243`; its launch QA and
  post-run storage maintenance passed. A later documentation-only publication
  must preserve this runtime profile.
- The authenticated HTML follow-up compared the unchanged v1 control, the
  HTTP/1.1 keep-alive client, the one-worker native server transport and the
  two-worker native profile. All pressure windows observed web-process
  replacement near the `1 GiB` cgroup limit and failed the declared stress
  acceptance; the detailed evidence and decision are in the
  [performance transport runbook](performance-transport-runbook.md).
- The reviewed diagnostic join fix is merged in [PR #86](https://github.com/StrayForest/old_sparky/pull/86).
  The corrected diagnostic run
  [`34512252295`](https://github.com/StrayForest/old_sparky/actions/runs/34512252295)
  produced `1,393` non-zero SSR↔API diagnostic joins, but is invalid as a
  performance baseline because it returned `15,504` HTTP 200 and `4,496` HTTP
  502 responses and replaced the web process twice. Its event-loop ELU was
  approximately `1.0` and web CPU averaged approximately `99%`; these are
  diagnostic-pressure signals, not an optimization result.
- The narrow timeout-only run
  [`34515991086`](https://github.com/StrayForest/old_sparky/actions/runs/34515991086)
  kept the standard runtime and exact `20,000 / 40 / c64 / no-retry` load
  contract. It recorded `49` client `TimeoutError` results. All `49/49` had
  a correlated Nginx record with status `200`, Next upstream status `200` and
  completed upstream processing; Nginx connect time was at most `4 ms` and
  origin request time at most `1,173 ms`. The client received no HTTP response
  or CF-Ray for any of these timeouts, while Nginx recorded a CF-Ray for every
  matching origin request. Derived from the second-precision Nginx timestamp,
  `43` origin requests started after the client timeout, `5` have ambiguous
  start ordering, and `1` origin request completed before the timeout.
- This localizes the reproduced timeout path outside the completed origin
  request, at the client↔Cloudflare delivery/forwarding boundary; the artifact
  cannot further split client socket behavior from Cloudflare edge behavior
  because no client response headers exist for a timed-out request. The
  timeout-only contour intentionally left SSR/event-loop and API request logs
  off, so Node scheduling is not proven as the cause of these client timeouts.
  No performance optimization or full correlated performance run is authorized
  until an unchanged baseline passes its declared contract.
- Exact fixture cleanup passed in the timeout-only run. Bounded storage
  maintenance [`34515587266`](https://github.com/StrayForest/old_sparky/actions/runs/34515587266)
  and final diagnostics
  [`34515762041`](https://github.com/StrayForest/old_sparky/actions/runs/34515762041)
  left the retained-load lock unlocked, no reclaimable release artifacts, and
  the active/previous release protection intact.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA. The canonical public-host policy is apex-only; `www.old-sparky.com` is intentionally unsupported and has no DNS alias.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Web, API and worker run under separate locked Unix identities with per-service runtime environments; the web process receives no backend database, session, R2, mail, Turnstile-secret or OpenAI credentials.
- API and worker share only the dedicated `oldsparky-media` staging group; worker state and web cache remain service-owned.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, durable patch-translation state, the rebuildable tournament catalog read model with keyset pagination, the admin roster control center, immutable releases and tested rollback. Google OAuth login/registration is implemented and enabled with operator-managed OAuth credentials.
- Frontend audit remediation for contract validation, permissions, auth/session states, async draft/search races, retry boundaries, internal navigation and i18n is resolved and deployed in release `frontend-audit-remediation-20260822T204107Z`; evidence is in [`archive/frontend-audit-remediation-2026-08-22.md`](archive/frontend-audit-remediation-2026-08-22.md).
- Cloudflare Access now protects `/platform-ops*` and `/api/v1/admin*` with an operator-scoped Allow policy and independent MFA; a fresh incognito login verified the identity -> TOTP MFA -> application path while application `admin`/`superadmin` RBAC remains authoritative.
- The full read-only Cloudflare API audit on 2026-09-05 confirmed apex-only DNS, active edge certificates, the Standard R2 bucket with `r2.dev` and browser PUT CORS disabled, the active `cdn.old-sparky.com` custom domain, and a Turnstile widget restricted to `old-sparky.com`. The reviewed catalog Cache Rule was repaired to bypass `__Host-old_sparky_session`; anonymous catalog requests are `HIT`, while session-cookie and `/mine` requests are `DYNAMIC`/uncached. AUD-02 remains open for the CAA decision, certificate alerts, media-token scope, WAF/rate-limit configuration, Bot Fight Mode runtime decision and range/UFW alerting; see [AUD-02](application-security-audit.md).
- Cloudflare is the single visitor-facing HSTS owner. Dashboard verification on 2026-08-21 confirmed HSTS On with six-month `max-age=15552000`, `includeSubDomains` Off and preload Off; Nginx must continue to omit HSTS.
- Cloudflare Full(strict), minimum visitor TLS 1.2, TLS 1.3/HTTP3 and DNSSEC were operator-confirmed on 2026-08-21.
- Invite-only tournament workspace reads reject retained or otherwise inactive participant records. Bracket data is delivered through the authorized workspace response and remains governed by the ordinary request authorization boundary.
- Organizer participant removal is a retained `disqualified` record rather than a physical participant-row deletion. A disqualified participant cannot redeem another invite or self-rejoin that same tournament, and a retry does not consume another invite use. The organizer-only management roster retains inactive rows for explicit restoration; the exclusion remains scoped to that tournament and does not block participation in unrelated tournaments.
- Tournament invite revocations and active participant-capacity mutations are transaction-serialized in PostgreSQL. Invite-code lookup is read-only and does not create personal access or consume a use; restoring a retained inactive participant rechecks capacity before making the row active again.
- Anonymous public profile contracts omit account/contact email and Steam authentication identity. Public tournament participant/workspace contracts omit moderation note, moderator identity and moderation timestamps; organizer management uses a separate response DTO that retains those fields.
- Public tournament automation errors are persistence-sanitized before commit: `automation_last_error` can contain only the stable generic retry message, while restricted logs retain only tournament/failure metadata and a one-way error fingerprint. Migration `20260821_0039` rewrites historical non-null values to the same safe message.
- Ready Check uses a deterministic timer contract: the tournament workspace carries `starts_at`, `ends_at`, eligible/current-user state and a UTC `server_time` anchor; the browser uses elapsed monotonic time to activate and expire the button locally without background requests. The vote POST revalidates server time, eligibility and workflow state under the durable concurrency rules, and a delayed automation worker cannot reject a valid in-window vote. `/tournaments` and the bracket grid remain request-driven; the initial bracket is included in the workspace response, and passive bracket changes appear after manual page reload. Redis remains available to unrelated platform services. See the [tournament timing ADR](adr/ready-check-and-bracket-boundary.md).
- Public media delivery is one-way `R2 -> CDN -> browser`: FastAPI exposes no `/api/v1/uploads/*` serving route, performs no render-path R2 object reads and has no R2-to-local-disk read fallback. Runtime serializers return only ready media-descriptor CDN URLs; historical `avatar_url`, `banner_url` and `cover_url` values are inert.
- Production releases are built in GitHub Actions as immutable, attested artifacts with an artifact-bound Python wheelhouse and digest; the VPS verifies the artifact/source commit and does not resolve dependencies or build from source.
- The production origin perimeter proof passed on 2026-09-05 for source SHA `97db79b681dd90cc8e89dd91f549610c943c16b8`: listener inventory, forwarded-header trust, Cloudflare/Nginx/UFW parity and external IPv4/IPv6 direct-origin blocking are recorded in [`archive/as-12-origin-perimeter-2026-09-05.md`](archive/as-12-origin-perimeter-2026-09-05.md).
- Unknown public patch IDs return from the cache path without awaiting external content refresh. Per-ID negative caching and a Redis-coalesced global background-refresh gate bound miss amplification, while miss-triggered upstream requests refuse redirects and enforce a response-size limit.
- Password-login guessing protection uses independent source-IP and account-wide Redis state. Account identifiers are represented by HMAC fingerprints, shared failures drive adaptive Turnstile and a bounded cooldown, and successful login clears account failure/cooldown state.
- Production Alembic head is `20260903_0052`, including Google external identities and browser-bound OAuth state alongside the tournament catalog
  read-model and keyset-pagination revisions. The migration scenario records
  this as the current head; see the [deployment runbook](deployment-runbook.md)
  for the exact release-SHA evidence.
- On 2026-08-24 production was reset only after a restore-verified backup
  (`platformdb-20260824T173357Z.dump`, SHA-256
  `3ee0e6616b4af7964578a02d1df9cbef2855b0559bec8a395d3435cd15c0379d`). The
  designated control account, its configured profile/media/access graph and
  roles were retained; all tournament links, tournaments and other application
  data were removed. Post-reset verification found one user, zero tournaments
  and zero participant/workflow/audit rows.

## Current engineering priority

The latest production performance stage completed on 2026-09-07 against
deployed SHA `bba3fb278e348906a6942aee8462b758c3d616ef`. The measurement
boundary and retained-load runtime contour were corrected, then the exact
13-profile matrix was rerun with the original contracts, thresholds and
dataset sizes; lifecycle profiles were not run on production. The complete
table, run links, status splits, origin-safety peaks and cleanup proof are in
the [archived performance-stage report](archive/performance-stage-2026-09-07.md).

All 13 profiles passed their declared acceptance and exact cleanup. Normal
Ready Vote/read traffic had no unexpected statuses, timeouts, 520s or 522s.
Stress profiles shed only with the declared `503 READY_VOTE_OVERLOADED`
response, and read profiles completed their 200/304 contracts. PostgreSQL
backend peaks were `51–52`, within the 52-backend budget, with ownership
consistency passing.

The authenticated control returned 20,000/20,000 HTTP 200 responses with total
p95 `3357.939 ms` and HTML TTFB p95 `2568.859 ms`. The D9 projection is
functionally clean and its stress contract passed, but this was not a
same-window unchanged-code A/B and it did not close the requested authenticated
TTFB target of `<1,000 ms`. The remaining owner-level priority is a bounded
authenticated web/API investigation; no worker or pool scaling is authorized
by this evidence.

The earlier stale-checkout attempt
([`34067801649`](https://github.com/StrayForest/old_sparky/actions/runs/34067801649))
failed closed before fixture creation. The first post-deploy control
([`34089756423`](https://github.com/StrayForest/old_sparky/actions/runs/34089756423))
passed its HTTP measurement but failed cleanup at the safe-env contour;
corrected cleanup [`34096318760`](https://github.com/StrayForest/old_sparky/actions/runs/34096318760)
removed the retained fixture completely. Subsequent controls and the full
matrix used the exact immutable `current` release with the shared
deployment/load lock.

The GitHub `Protect dev` ruleset no longer requires PR approval for merge.
Branch deletion and force-push remain protected, and exact-SHA CI/build plus
the automatic production deployment chain remain mandatory.

The reviewed transport-observability package was introduced in deployed source
SHA `9c5ac7e59466e021bc5ce8142721e4027500407e` through automatic production
deploy [`34362338793`](https://github.com/StrayForest/old_sparky/actions/runs/34362338793).
It scopes Nginx response-buffering changes to the dynamic HTML location,
records upstream/client transport headers, adds opt-in Node event-loop/CPU/GC
diagnostics, provides the bounded same-request hop probe, and exposes a
read-only exact-window web-runtime journal collector. This is an
instrumentation and transport-policy deployment, not an accepted TTFB result.

The clean control
[`34334229164`](https://github.com/StrayForest/old_sparky/actions/runs/34334229164)
recorded two `deadlock-web` process replacements during the 20,000-request
window and ended with `3,539` HTTP 502 responses; exact cleanup passed. The
same-source compression-off A/B
[`34339014480`](https://github.com/StrayForest/old_sparky/actions/runs/34339014480)
worsened TTFB p95 to `2,829.887 ms` and added client timeouts, so compression
remains enabled after restore
[`34340974394`](https://github.com/StrayForest/old_sparky/actions/runs/34340974394).
The read-only runtime diagnostics
[`34344738197`](https://github.com/StrayForest/old_sparky/actions/runs/34344738197)
confirmed two systemd cgroup OOM kills of `next-server` at approximately
`1,029,412 KiB` and `1,029,144 KiB` under `MemoryMax=1G`; the resulting
automatic restarts explain the 502s and secondary queueing. Full evidence is in
the [transport investigation archive](archive/performance-authenticated-html-ttfb-transport-2026-09-09.md).
The bounded direct Node HTTP transport was measured only as an opt-in candidate,
together with a bounded two-worker Node cluster, by external run
[`34357443978`](https://github.com/StrayForest/old_sparky/actions/runs/34357443978):
20,000/20,000 HTTP 200, zero client errors, no web process churn and exact
cleanup. TTFB p95/p99 was `1173.196/1359.348 ms`; Nginx upstream-header p95 was
`899 ms`, web RSS peaked at about `249 MB`, and the two host CPUs averaged about
`88%`. The candidate passed the declared stress contract and materially reduced
the OOM/queueing failure mode, but it missed the `<1,000 ms` target by `173.196
ms`, so it is not the production default. The direct transport is now gated by
`PLATFORM_WEB_SERVER_AUTH_TRANSPORT=node` and the two-worker profile; ordinary
baseline/static/diagnostic profiles explicitly use `fetch`. The current
standard production runtime is the restored `ready-vote-static-8` baseline,
with compression enabled and Node `26.3.1`, validated by
[`34550534777`](https://github.com/StrayForest/old_sparky/actions/runs/34550534777)
from behavior-bearing SHA `0700b7402ecdd182fe0cfba4feae14f15fb68243`.

The 2026-09-11 authenticated HTML follow-up did not produce a promotion
candidate. The unchanged v1 control returned `18,077/20,000` HTTP 200 and
`1,923` HTTP 502 responses (`9.615%` final failure); the v2 keep-alive client
returned `15,938` HTTP 200, `4,061` HTTP 502 and one client no-response result;
the one-worker native transport returned `17,549` HTTP 200 and `2,451` HTTP
502; and the two-worker native profile returned `14,762` HTTP 200 and `5,238`
HTTP 502. Database ownership/lock safety checks passed in every window, while
the web process was replaced and RSS approached the `1 GiB` cgroup limit.

Remaining performance work is explicit: authenticated page TTFB remains above
the `<1,000 ms` target. The prior blocked attribution is retained in the
[`authenticated HTML/TTFB archive`](archive/performance-authenticated-html-ttfb-2026-09-07.md),
and the root component-tree diagnostic is archived in
[`performance-authenticated-html-ttfb-root-component-tree-2026-09-08.md`](archive/performance-authenticated-html-ttfb-root-component-tree-2026-09-08.md).
Its exact run measured 194 correlated requests and identified the largest
measured pre-body interval as root response serialization/flush scheduling,
without a server data-ready marker. The completed transport investigation and
timeout-path diagnosis are documented in
[`performance-transport-runbook.md`](performance-transport-runbook.md). Cloudflare
body buffering and the same-source Next.js compression candidate have now been
checked; the latter was rejected. The transport A/B remains directional
evidence, not a reason to change production. The root render/flush boundary
remains frozen. The correlation instrumentation is present in reviewed `dev`
SHA `08862794fe84becc664ba4b6dae5b9d920e06723`. The first safe-profile diagnostic
load [`34448478660`](https://github.com/StrayForest/old_sparky/actions/runs/34448478660)
was invalid for latency attribution because it returned `2,783` HTTP 502s and
its sampled SSR/API join was `0/0`; exact cleanup still removed all `20,000`
users and `40` tournaments. After an explicit diagnostic-only correlation
bridge was reviewed and deployed, the repeat
[`34461377765`](https://github.com/StrayForest/old_sparky/actions/runs/34461377765)
returned `18,289` HTTP 200 and `1,711` HTTP 502 responses, so its stress
acceptance is also invalid. The observer completed without timeout and recorded
`188` sampled SSR requests (`187` correlated HTML rows), but the auth/API join
remained `0/0`. It measured auth bootstrap p95 `68.933 ms`, API sampled
request p95 `194.753 ms` with pool checkout p95 `157.572 ms`, and Node
event-loop CPU/GC pressure; these are directional aggregate signals, not a
per-request split. A synthetic loopback probe confirmed that the explicit
headers reach the API and are selected by its diagnostic middleware, so no
auth-path, SSR-cost or scheduling optimization is authorized until the real
load join is non-zero.
The baseline was restored to `ready-vote-static-8` by
[`34464396511`](https://github.com/StrayForest/old_sparky/actions/runs/34464396511)
on the same SHA with `fetch` transport and both diagnostic log gates disabled.
All services and health checks returned 200. Exact fixture cleanup left no
retained load data, and the subsequent maintenance run produced a
restore-verified backup while bounded release retention left approximately
`9.8 GiB` free; `current` and `previous` remained protected.

The unchanged baseline on the current release was rerun as external workflow
[`34478322962`](https://github.com/StrayForest/old_sparky/actions/runs/34478322962)
with the fixed authenticated-page contract: `19,948` HTTP 200 responses and
`52` client `TimeoutError` results, with zero 502s, OOMs or web restarts. Client
TTFB p95 was `2679.517 ms`; web CPU averaged `91.66%`, while PostgreSQL showed
no lock waiters or connection-budget contention. Diagnostics were off, so this
run had no per-request IDs. The timeout-only follow-up is the current evidence
for the reproduced timeout path; its non-zero 502/timeout result is not a valid
baseline and must not be used to justify optimization.
The security maintenance release upgraded Next.js to `16.3.4`, with its
exact-SHA CI and production evidence archived in
[`security-web-dependencies-next-2026-09-09.md`](archive/security-web-dependencies-next-2026-09-09.md).
Because framework and streaming behavior can change across that upgrade, the
new unchanged Next.js 16.3.4 control is
[`authenticated-page-load-v1` run 34287694375](https://github.com/StrayForest/old_sparky/actions/runs/34287694375):
20,000/20,000 HTTP 200, TTFB p95 `1725.436 ms`, full-page p95
`2279.541 ms`, PostgreSQL max `41/52`, and exact cleanup with zero remnants.
The old `1325.095 ms` TTFB result remains historical diagnostic evidence and
must not be used as the A/B control for the next candidate.
The browser-workspace candidate is archived in
[`performance-authenticated-html-ttfb-workspace-client-2026-09-08.md`](archive/performance-authenticated-html-ttfb-workspace-client-2026-09-08.md),
and the avatar candidate is archived in
[`performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md`](archive/performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md).
The bounded admission/pool-contention candidate was rejected after exact A/B
run `34137667234`: TTFB p95 was `2356.822 ms` with all 20,000 HTTP responses
successful and exact cleanup. Production is restored to
`authenticated-read-admission-32`. The first overlap candidate was deployed in
release `4db0079fcddba6fa37bd089d2a96599c04d02768` and measured by exact
external run `34145804669`: `20,000/20,000` HTTP 200, exact cleanup, but HTML
TTFB p95 `2527.624 ms`, only `1.605%` below baseline, so it was not accepted
as the target solution. SSR-only diagnostics in run `34148261104` measured
post-data unattributed upstream p95 `2340.311 ms` after detail data-ready p95
`1112.536 ms`; the archived route-local Suspense candidate improved TTFB p95 to
`2325.559 ms` in exact run `34153656342` but did not close the target. The
rejected client detail-boundary candidate was measured by exact run
`34159422212`: it returned `20,000/20,000` HTTP 200 responses with exact
cleanup and TTFB p95 `2270.945 ms`, a safe incremental result that did not
close the target. The archived candidate deferred the tournament workspace read
to the browser while retaining the server-rendered authoritative auth/header
path. Its exact A/B was `34169435362`; its diagnostic repeat was `34171146327`;
production was restored to `ready-vote-static-8` by `34172262955`. The avatar
candidate then removed only the optional avatar SQL and measured TTFB p95
`1,512.860 ms` in exact run `34174264965`, with fresh attribution in
`34175851102`. The chrome boundary candidate is archived in
[`performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md`](archive/performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md):
both exact 20k/40 attempts failed to complete the client load and were cleaned
successfully. A post-rollback diagnostic load also stalled before a client
report; guarded abort and exact cleanup passed for its one marker, 20,000
users and 40 tournaments with zero remnants. Production was restored to
`ready-vote-static-8` by deploy `34184805970` for reviewed SHA
`3d7aca0e832bac3c79e1b391e7817b74e5695b03`. The active diagnostic work order
above is now the only next performance step; the narrower
authenticated-provider-boundary candidate is
archived in
[`performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md`](archive/performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md).
Its reviewed SHA `6e3a6325a0c6bee025297a8c8edb00817faafb75` completed the exact
20k/40 profile in run `34192721178` with 20,000/20,000 HTTP 200, zero errors,
zero unexpected/overload/retry responses, backend peak `43/52`, zero lock
waiters, and exact cleanup. It reduced HTML TTFB p95 to `1341.322 ms`
(`47.8%` below the unchanged `2568.859 ms` control), but remained `341.322 ms`
above the `<1,000 ms` target. It was therefore reverted through the reviewed
`dev` path. The follow-up global-chrome boundary candidate is archived in
[`performance-authenticated-html-ttfb-global-chrome-2026-09-08.md`](archive/performance-authenticated-html-ttfb-global-chrome-2026-09-08.md).
Its reviewed source SHA `0cb0f1fabafa793d0520774a04883b02ad4a2584` completed
the exact 20k/40 profile in run `34216385147`: all 20,000 responses were HTTP
200 with zero errors, unexpected statuses, timeouts, overloads or retries;
HTML TTFB p95 was `1391.415 ms`, page p95 `1704.678 ms`, PostgreSQL peak `44/52`,
zero lock waiters, two workers and exact cleanup. It passed origin safety but
missed the target by `391.415 ms` and was worse than the narrower provider
candidate, so it is rejected. The candidate was reverted through the reviewed
`dev` path. Production was restored to `ready-vote-static-8` by automatic
deploy `34234377917` for rollback SHA `fe48877e3c7e10549bdd4a50afad5d5a9744b2f6`.
The tournament-detail-footer candidate is archived in
[`performance-authenticated-html-ttfb-detail-footer-2026-09-08.md`](archive/performance-authenticated-html-ttfb-detail-footer-2026-09-08.md).
Its exact load `34230327945` reached the full 20k/40 fixture but failed closed
on a client `IncompleteRead` before producing a TTFB report; exact cleanup
passed with zero remnants. No capacity-setting change is authorized while the
active diagnostic work order is incomplete.
The historical v3 timeout plus anomaly
`33991798604` remain unexplained transient episodes. No worker/pool increase or
external Cloudflare root cause is asserted without further evidence.

### Historical AS-18 context

AS-18 — hot-path capacity and backpressure implementation is complete.
Production remains commit- and exact-SHA-gated; detailed load output belongs in
external retained reports and is not a product architecture contract. The
reviewed Ready Vote path now also has a process-local adaptive admission
controller per API worker, with code defaults `4/8/16`, bounded/no-waiter
overload shedding before DB checkout, and a browser chain of at most two
jittered retries for the explicit overload response. Production is pinned to
`ready-vote-static-8` with exact per-worker limits `8/8/8`; workers, pool and
database budgets are unchanged. The corrected canonical performance model uses
`ready-vote-slo-v2`, `ready-vote-capacity-ramp-v2`,
`ready-vote-stress-15k-v2` and `ready-vote-spike-v1`; the optional 20k stress
profile is retained only for a specific unresolved question.

Current status: migration `20260903_0052` is the deployed Alembic head. The
`0048` revision adds a partial covering index for the `UserSession` auth query;
its `EXPLAIN` `Index Only Scan` / `Heap Fetches 0` result is disposable
engineering evidence, not a production architecture claim. The supported
load run `33335115575` passed with accepted p50/p90/p95/p99
`241.711/256.963/264.706/639.338 ms`, zero shedding/retries/final failures,
and exact cleanup. The supported-load and earlier adaptive/spike records
remain historical. The current fast-path baseline is SHA
`6580f7bf5c02641a8ff607c35bcc050e24b1a50e`, with SLO capacity `70 actions/s`
and knee approximately `80 actions/s`. Static-8 saturation sweeps
`33368575458`/`33374294139` established a canonical maximum stable goodput of
approximately `116 actions/s`, with the goodput plateau beginning in the
`120–130/s` offered band. The candidate Ready Vote upsert compiler
optimization is source SHA `68eb3f421049f8135bdf3b72c723dc4d93c8f57f`; its
A/B runs `33379397589`/`33381896491`/`33385667381` retained SLO capacity
`70/s`, knee `~80/s` and established a conservative candidate plateau of
approximately `117/s` goodput in the 120–135/s band with bounded origin
pressure. Full tables, profile evidence, rejected hypotheses and run links are
retained in `platform/performance/README.md`. The optional 20k stress profile
was not run. The SHA above is a benchmark baseline for the Ready Vote
comparison, not the current deployed source.
The supported-load SLO remains accepted p50/p90/p95/p99 <= 250/400/600/1000
ms, logical p95/p99 <= 600/1000 ms, final logical failure <0.5%, and
approximately zero normal-load shedding. Every run passed exact cleanup; the
designated control account remained intact. Profile versions/digests and full
tables are retained in `platform/performance/README.md`.

The current Ready Check implementation uses the initial workspace timing
contract: the page receives `starts_at`, `ends_at`, eligible/current-user
state and a UTC `server_time` anchor. The browser derives a server-relative
monotonic timeline and changes the button locally at the two boundaries. The
vote endpoint remains authoritative and validates time, eligibility,
workflow state, idempotency and concurrency. The active load gate is the
supported SLO profile, sustained capacity ramp, separate 15k stress profile
and explicit spike/recovery profile, each followed by exact cleanup.

The tournament catalog and bracket grid are request-driven. Public and personal
catalog cards are served from the rebuildable PostgreSQL
`tournament_list_read_models` projection: the source tables remain authoritative
and committed tournament, participant, workflow, profile and media changes
refresh the affected card. Catalog pages use cursor/keyset pagination and
`LIMIT + 1`; public responses have a five-second Redis response cache and emit
short-TTL origin cache headers. A live probe on 2026-09-05 verified the
Cloudflare catalog cache behavior: anonymous requests produced
`CF-Cache-Status: MISS` followed by `HIT` with `Age: 0`, while a request
carrying the actual production session cookie `__Host-old_sparky_session` and
a request carrying `Authorization` both returned `DYNAMIC`.
`/tournaments/mine` remains private and uncached. The initial workspace includes the bracket, passive changes
become visible after a manual page reload, and explicit organizer mutations
may refresh their own authoritative result.

The current load-test gate is request-based and is defined by the reviewed
profiles under `platform/performance/profiles/`: read profiles measure
authenticated catalog/tournament reads plus conditional manual workspace
reloads, while Ready Vote profiles measure vote POSTs. No background tournament
transport is part of the production flow. Public capacity measurements use the
external runner workflow `platform-production-external-load.yml`: deterministic
fixture setup is performed on the origin, the HTTP measurement runs outside the
VPS, and a bounded origin observer records API/PG/Redis/system pressure. The
external-load workflow is the only supported retained-load measurement path; it
is a manual operator gate, never ordinary CI, and every run requires exact
cleanup or abort handling before another run.

The current production read-mix winner is source
`4be82f1a9e682fda8bee990667b962f1f46e0b58`, deployed by
`33624720919`. It retains the revision-based single-query conditional 304
preflight and combines tournament base data, authenticated viewer
access/commitment data and the Ready Check common state plus viewer vote into
one workspace preflight for requests without an invite code. Canonical
production workflow `33625164162` passed with 20,000 users, 40×500
tournaments, 20,000 successful `200` reads and 10,000 valid `304` refreshes;
wall time was `301.757 s`, raw p95/p99 was `2382/3492 ms`, and API cores
averaged `98.40%`/`98.58%`. Workspace diagnostics measured `4.018`
SQL/request, `228.691 ms` average DB time and `1659 ms` pool checkout p95,
versus the previous `4.501`, `278.235 ms` and `2068 ms`. PostgreSQL lock
waiters stayed at zero, and exact cleanup removed all fixture users,
tournaments, sessions and audit rows while preserving the control account.
The earlier conditional-detail fast path and diagnostic sampling candidates
remain rejected historical experiments; this accepted candidate must continue
to be proven against the same profile without weakening authorization, ETag
correctness or exact cleanup. The follow-up candidate `6344168a` deferred
cached-session validation into the conditional workspace preflight. It reduced
workspace diagnostics directionally, but canonical runs `33647757275` and
`33650029282` both failed the full stress gate (client failures in the first
run; one `URLError` and PostgreSQL connection maximum `55` versus the `52`
origin-safety ceiling in the repeat). It was reverted by `38c06fda` and is not
the production runtime.

The read-path candidate slice was accepted on exact-SHA external evidence. It
adds connection-hold diagnostics, a short DB lease for the workspace and
full-user routes, the smaller `/auth/bootstrap` SSR dependency,
column-oriented workspace snapshots, explicit Nginx upstream keepalive and
selectable `uvloop`/`httptools`, pool-pre-ping and authenticated-read-admission
experiments. The `read-mix-concurrency-ramp-v1` run measured the full
20,000-user read mix at c16/c32/c48/c64/c80/c96/c112/c128: c32 was the stable
latency knee and c48 the first queued stage. The operator-selected
`authenticated-read-admission-32` profile is now active in production; the
API remains at two workers with pool size `24`, `max_overflow=0`,
`pool_pre_ping=true`, and the PostgreSQL safety budget remains `52`.
The ramp is operator-only and requires the existing exact fixture
cleanup/abort procedure.
The current dev candidate also removes the three tournament router-level
policies from public catalog and `/mine` resolution. Private child GETs,
invite claims and the affected roster/join mutations declare only the policy
they need, so a cached public catalog hit does not first resolve an unrelated
auth/DB dependency graph. Public and personal catalog endpoints now return the
compact `TournamentCardResponse`; detail/workspace responses retain the full
`TournamentResponse` contract. `/users/me` merges authoritative session
identity/roles/credits with a 60-second, versioned supplemental account
read-model. Its small monthly quota count is refreshed authoritatively from
PostgreSQL on cache hits; Redis does not decide authentication or
authorization.
The current deployed candidate makes profile-read cache access GET-first on a
process-local shared Redis pool and stores absent profiles as a versioned
60-second negative sentinel. Profile creation and profile mutations invalidate
that entry after commit. Exact production measurement
[`33760930891`](https://github.com/StrayForest/old_sparky/actions/runs/33760930891)
completed with 20,000 successful HTML responses and exact cleanup; the
isolated bootstrap probe confirmed one profile SQL for repeated absent-profile
requests and GET-only warm positive hits. Full evidence and the remaining
unmeasured hypotheses are retained in `platform/performance/README.md`.

The external profile `authenticated-page-load-v1` measures authenticated
`GET /tournaments/{slug}` HTML through the real Next.js origin and reports HTML
TTFB, total page latency, response bytes and origin SSR/API/PG evidence. The
observer labels PostgreSQL activity by `oldsparky-api`, `oldsparky-worker`,
`oldsparky-qa`, `oldsparky-observer` and `oldsparky-maintenance`, making the
connection budget attributable. The ramp report emits a latency-knee
recommendation for admission review; it never changes runtime limits
automatically. The authenticated HTML benchmark confirmed that admission 32
materially reduces SSR-origin queueing: full-page p95/p99 improved from
`10378/10714 ms` at c64 to `3906/4425 ms`, while pool checkout p95 improved
from `10001 ms` to `533 ms`. The isolated `pool_pre_ping=false` A/B was rejected
and production was restored to `pool_pre_ping=true`.

The diagnostic page run `33738366863` on exact SHA
`30f49f2b8997b6b4d3049f889290946dad22ac82` completed with 20,000 successful
HTML responses and no shedding: full-page p95/p99 was `4073/4693 ms`, HTML
TTFB p95/p99 was `3529/3852 ms`, and origin PostgreSQL ownership peaked at
`48 api + 2 worker + 1 observer = 51`. Sampled SSR stages show that
`auth_bootstrap_fetch` dominates at p95 `2136 ms`, while workspace is p95
`1310 ms`; Node event-loop p95 was `133 ms` and the web process averaged about
`65%` CPU. The matching bootstrap stage counts confirm React `cache()` performs
one actual bootstrap fetch per sampled request across RootLayout and page.
The SSR bootstrap now keeps PostgreSQL-authoritative session/role validation
but skips read-only page renders' `last_seen_at` telemetry write and its extra
DB checkout. This does not move authentication authority to Redis.

The follow-up A/B run `33744391709` on exact SHA
`1861b983216d8ae6b63295ab60bf22b5a6bbeda2` confirmed the change under the same
20,000-request profile: `/bootstrap` p95/p99 improved from `3346/4114 ms` to
`2602/2934 ms`, average SQL count fell from `3.0` to `2.0`, and pool checkout
p95 fell from `638 ms` to `472 ms`. Full-page p95/p99 moved from
`4073/4693 ms` to `3901/4426 ms`; HTML TTFB p95/p99 moved from
`3529/3852 ms` to `3423/3796 ms`. PostgreSQL ownership remained `48 api + 2
worker + 1 observer = 51`, while Nginx `request_time_ms` is now populated by
the observer. The run passed acceptance with zero shedding/retries, and exact
cleanup removed all 40 tournaments and 20,000 users with no fixture rows left.

The `web-ssr-diagnostics` operator profile adds bounded 1% request-correlated
Next.js stage timings, five-second Node event-loop samples and Nginx HTML
timing correlation to the retained observer artifact. It is diagnostic-only
and does not alter SSR behavior, authorization, or the nonce CSP. Public/static
HTML remains deferred and is not part of this change; the nonce CSP is not
bypassed or weakened.
Production service logs are kept in journald, Nginx owns the edge access log,
and size-based rotation bounds text log files.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only workspace reads require active participant membership or explicit organizer/admin authority; historical inactive participant rows are not authorization grants.
- Organizer exclusion must retain the tournament participant row as `disqualified`; self-rejoin and same-tournament invite redemption remain blocked until the organizer deliberately restores an active status. This is tournament-scoped and must not become a platform-wide ban.
- Participant capacity and invite revocation are transaction-scoped PostgreSQL invariants: ordinary code lookup is read-only, while ordinary joins claim durable free slots without locking the tournament row and lifecycle/restore mutations retain the tournament-row boundary and recheck capacity. Authentication last-seen touches use an isolated database transaction and must never commit or release locks owned by a mutation request.
- Resource-creating API retries use durable actor/scope `Idempotency-Key` records. A repeated key with the same payload resolves to the originally created tournament/invite; reusing a key with a different payload is rejected.
- Player-commitment reconciliation is a tournament workflow writer: it locks every affected Tournament row in deterministic id order before reading lifecycle state or releasing commitments. Automation failure-state persistence reacquires the same Tournament lock after any rollback.
- Every Deadlock ready-check start/close, captain, assignment generation, roster
  publish and roster-lock write path — API, automation and worker alike — locks
  its tournament row before checking lifecycle state. Ordinary ready votes are
  the deliberate exception: they upsert the unique vote row and its 128-way counter
  shard without taking the tournament-row lock; a deferred guard rejects
  post-close or ineligible votes while preserving a vote timestamped before the
  close commit. Redis may coalesce work but never replaces this durable boundary.
- Participant capacity is represented by durable per-tournament slots. Join
  claims a free slot with `FOR UPDATE SKIP LOCKED`; inactive retained rows and
  deletes release capacity, while the unique `(tournament_id, user_id)` index
  and idempotency record guard retries. The table materializes a bounded
  inventory and allocates sparse rows above it on demand, so the permitted
  nine-digit API capacity cannot trigger a massive slot backfill.
- Bracket/workspace reads expose revision-derived private ETags and accept
  `If-None-Match`; unchanged manual reloads return `304`. The browser updates
  passive bracket state only after a manual page reload. Explicit bracket
  mutations may refetch their authoritative response.
- API and worker SQLAlchemy pools are explicit and bounded within the ordinary
  connection budget. Celery uses high/default/low queues, prefetch one and
  late acks; backlog/retry pressure is evidence. The reviewed Ready Vote
  profiles own join/ready-vote contention measurements.
- Ready-check votes must be committed only while their round is active and the voter remains an eligible active participant; a close or exclusion
  cannot leave a post-close or ineligible vote in persistence.
- The database is the final concurrency guard for cardinal workflow state:
  active ready-checks and the selected captain/assignment/roster state must not
  have ambiguous concurrent rows even if a future writer bypasses a service.
- Dream-slot replacement is serialized on the owning profile/user row. A
  replace-all request leaves exactly its selected profile-level slots, never a
  merge of concurrent payloads; slot values remain in the supported range.
- Public API contracts are explicit allowlists. Account/contact email and Steam authentication identity do not belong to anonymous public-profile DTOs, participant moderation metadata belongs only to organizer-management DTOs, and public automation error fields must never contain arbitrary exception text. A future public email feature requires a separate explicit opt-in contract rather than reusing account contact data.
- Bracket/workspace access must remain authorized by the ordinary request
  boundary. The active grid is request-driven; Redis is not a bracket
  dependency.
- Public media rendering must remain `R2 -> CDN -> browser`; normal API runtime must not proxy R2 objects, serve legacy upload paths or fall back to local-disk reads. Legacy URL response fields and database columns remain only as compatibility/data-migration fields: runtime serializers ignore their stored values, while migration diagnostics and reconciliation may still use them. Physical removal requires a reviewed API/schema migration after production data and consumer inventory.
- Unknown public patch IDs must not make the request path wait on external refresh work. Retain per-ID negative caching, cross-worker refresh coalescing and explicit no-redirect/response-size bounds for miss-triggered upstream requests.
- Password-login protection must retain independent per-IP and account-wide buckets. Account-wide Redis state must use private HMAC fingerprints rather than plaintext identifiers; cooldowns remain bounded and must not extend on blocked requests, and a successful login clears the account failure/cooldown state.
- Cloudflare Access is defense in depth only: privileged application RBAC remains authoritative after edge authentication/MFA succeeds.
- Cloudflare remains the single HSTS owner; do not add `Strict-Transport-Security` at Nginx while this ownership model is active.
- Terminal tournament states freeze organizer match administration.
- Preserve the live HTTPS/domain/secure-cookie contour unless a reviewed release changes it.
- Rollback switches application releases; it does not automatically downgrade Alembic.
- New production changes use the durable release state machine in
  [`release-state-machine.md`](release-state-machine.md); a post-migration
  failure retains a recovery receipt and blocks an unrelated second install.
- Canonical production env remains root-only `root:root 0600`; preflight checks
  that scoped service env files are freshly rendered from it.
- Normal production deployment is automatically dispatched only after the
  `Platform security and build` workflow completes successfully for a push to
  the current `dev` HEAD. The auto-deploy gate refuses stale successful CI
  results and skips SHAs that already report `platform-production-deploy=success`.
  The manual `Platform production deploy` workflow remains an operator fallback;
  direct server invocation is recovery/rollback-only.

## Deferred / operator-owned work

- Remaining Cloudflare operator follow-up is limited to media-token scope,
  Universal SSL lifecycle-alert availability and Managed WAF entitlement.
  CAA, Certificate Transparency alerting, the plan-compatible login edge rate
  limit, the deliberate Bot Fight Mode-disabled decision and the daily
  Cloudflare-range/UFW alert workflow have fresh operator/live evidence. DNS,
  edge certificates, R2, Turnstile hostnames and the catalog cache boundary
  also have fresh API/live evidence; see the AUD-02 archive.
- Real-user CSP follow-up and classification of new enforcement reports.
- Physical removal of persisted legacy media URL fields and migration-only helpers after production data and external-consumer inventory confirms that no migration or compatibility dependency remains; this requires a reviewed API/schema migration.
- Non-security feature expansion that does not remove a launch or production blocker. For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md); for evidence and details, follow [`README.md`](README.md).
