# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-08-27

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Web, API and worker run under separate locked Unix identities with per-service runtime environments; the web process receives no backend database, session, R2, mail, Turnstile-secret or OpenAI credentials.
- API and worker share only the dedicated `oldsparky-media` staging group; worker state and web cache remain service-owned.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, immutable releases and tested rollback.
- Frontend audit remediation for contract validation, permissions, auth/session states, async draft/search races, retry boundaries, internal navigation and i18n is resolved and deployed in release `frontend-audit-remediation-20260822T204107Z`; evidence is in [`archive/frontend-audit-remediation-2026-08-22.md`](archive/frontend-audit-remediation-2026-08-22.md).
- Cloudflare Access now protects `/platform-ops*` and `/api/v1/admin*` with an operator-scoped Allow policy and independent MFA; a fresh incognito login verified the identity -> TOTP MFA -> application path while application `admin`/`superadmin` RBAC remains authoritative.
- Cloudflare is the single visitor-facing HSTS owner. Dashboard verification on 2026-08-21 confirmed HSTS On with six-month `max-age=15552000`, `includeSubDomains` Off and preload Off; Nginx must continue to omit HSTS.
- Cloudflare Full(strict), minimum visitor TLS 1.2, TLS 1.3/HTTP3 and DNSSEC were operator-confirmed on 2026-08-21.
- Invite-only tournament workspace reads reject retained or otherwise inactive participant records; private bracket SSE also revalidates active participant membership while the connection remains open, so withdrawal/disqualification revokes an existing stream before further private events are emitted.
- Organizer participant removal is a retained `disqualified` record rather than a physical participant-row deletion. A disqualified participant cannot redeem another invite or self-rejoin that same tournament, and a retry does not consume another invite use. The organizer-only management roster retains inactive rows for explicit restoration; the exclusion remains scoped to that tournament and does not block participation in unrelated tournaments.
- Tournament invite claims/revocations and active participant-capacity mutations are transaction-serialized in PostgreSQL. Last invite use and last participant slot cannot be consumed twice, and restoring a retained inactive participant rechecks capacity before making the row active again.
- Anonymous public profile contracts omit account/contact email and Steam authentication identity. Public tournament participant/workspace contracts omit moderation note, moderator identity and moderation timestamps; organizer management uses a separate response DTO that retains those fields.
- Public tournament automation errors are persistence-sanitized before commit: `automation_last_error` can contain only the stable generic retry message, while restricted logs retain only tournament/failure metadata and a one-way error fingerprint. Migration `20260821_0039` rewrites historical non-null values to the same safe message.
- Public bracket SSE connection pressure is bounded in two layers: Redis-backed application leases enforce global, source and authenticated-user admission caps with fail-closed behavior, while Nginx adds coarse source/global connection caps. The deployed AS-19 package uses an application global ceiling of `3,000` with `32/source` and `4/user`, while Nginx retains `10,240` source/global ceilings and `worker_connections=32,768`; signed short-lived admission tickets remove PostgreSQL from the ticketed open path, relay fan-out is shared per worker/tournament, and the limiter pool is bounded at `512` connections with a `2s` wait. Private streams retain periodic session/participant revalidation and all Redis leases remain fail-closed. The lower application ceiling is a controlled-degradation guard against the observed Cloudflare Error 1200 edge queue, not a claim that 3,000 is final customer-facing capacity.
- Public media delivery is one-way `R2 -> CDN -> browser`: FastAPI exposes no `/api/v1/uploads/*` serving route, performs no render-path R2 object reads and has no R2-to-local-disk read fallback. Runtime serializers return only ready media-descriptor CDN URLs; historical `avatar_url`, `banner_url` and `cover_url` values are inert.
- Production releases are built in GitHub Actions as immutable, attested artifacts with an artifact-bound Python wheelhouse and digest; the VPS verifies the artifact/source commit and does not resolve dependencies or build from source.
- Unknown public patch IDs return from the cache path without awaiting external content refresh. Per-ID negative caching and a Redis-coalesced global background-refresh gate bound miss amplification, while miss-triggered upstream requests refuse redirects and enforce a response-size limit.
- Password-login guessing protection uses independent source-IP and account-wide Redis state. Account identifiers are represented by HMAC fingerprints, shared failures drive adaptive Turnstile and a bounded cooldown, and successful login clears account failure/cooldown state.
- Production Alembic head before this release is `20260822_0040`; the reviewed
  branch target adds `20260824_0042` participant slots and `20260824_0043`
  ready-vote lifecycle guards.
- On 2026-08-24 production was reset only after a restore-verified backup
  (`platformdb-20260824T173357Z.dump`, SHA-256
  `3ee0e6616b4af7964578a02d1df9cbef2855b0559bec8a395d3435cd15c0379d`). The
  account `aleksei.lisitsin1@gmail.com`, its configured profile/media/access
  graph and roles were retained; all tournament links, tournaments and other
  application data were removed. Post-reset verification found one user,
  zero tournaments and zero participant/workflow/audit rows.

## Current engineering priority

AS-18 — Hot-path capacity and backpressure implementation and local capacity
verification are complete. The scope and execution checklist are maintained in
[`archive/as-18-hot-path-capacity-backpressure-plan-2026-08-24.md`](archive/as-18-hot-path-capacity-backpressure-plan-2026-08-24.md).
The protected-account/database reset gate is resolved for the supplied
production identity. Migration, exact-SHA CI, deploy smoke and retained-load
evidence remain release gates. The canceled browser-polling run on 2026-08-24
also exposed an operator-contour bug: canceling the GitHub SSH step did not
propagate to the remote supervisor. The reviewed abort workflow, remote
180-minute ceiling and durable-report recovery are now part of the retained
load procedure; a canceled run remains a failed measurement and must be
cleaned exactly. A production browser setup timeout also exposed the
create-before-response boundary: the exact cleanup path now recovers only a
marker-matching tournament owned by that run's synthetic organizer set before
deletion, while malformed or foreign matches remain fail-closed.

The measured local browser-polling staircase reached 1,000 → 5,000 → 10,000
virtual users on the selected bounded profile. Production remains commit- and
exact-SHA-gated;
a canceled, recovered or setup-failed run is not a successful benchmark. The
first production browser run (`32798245204`) created its 10,000 users but hit a
Cloudflare 504 while creating the first tournament, before polling began. Its
exact cleanup (`32799479496`) deleted 10,000 users and 1 partial tournament,
verified zero fixture users/tournaments/sessions/audit rows and preserved the
control account. A production polling pass is therefore not claimed yet.
The repeat production run (`32800341184`) completed all 20 tournaments and
12,283 polling GETs, but observed zero `304 Not Modified` responses with p95
58.5s/p99 98.5s; its exact cleanup (`32800905099`) removed 10,000 users and
20 tournaments. The production wrapper was then switched to the measured
active/passive browser mix.
The mixed run's zero 304 result is consistent with Cloudflare rewriting strong
ETags as weak validators; the API now uses RFC-compatible weak comparison and
has regression coverage. The post-fix release at `ca2960bd` passed security/build
(`32802478200`), automatic deploy (`32802841513`) and production smoke
(`32802847059`). Its retained 10,000-user production gate (`32803100629`)
completed all 20 tournaments and 11,659 polling GETs, including 1,201
conditional `304` responses, with p95 433ms/p99 700ms and no sustained CPU,
connection or lock saturation. Exact cleanup (`32803657743`) deleted 10,000
users and 20 tournaments, left zero fixture users/tournaments/sessions/audit
  rows and preserved the control account. The transport ten-hypothesis matrix
  and its five follow-ups are complete. The current combined-load winner is the
  frontend-aligned lean workspace contract with client pool512 and a 600s
  opening stagger; F2 was selected by zero errors first, then p95/p99, CPU and
  pool wait rather than raw throughput.

The current SSE harness follow-up isolates transport from fixture workflow
latency. SSE and combined profiles use the requested synthetic-user count and
one public tournament on a single Redis hot key; they do not run the full
ready-check/assignment fixture first. Setup is bounded at 90 seconds. The
full 20-tournament stateful fixture remains owned by the browser-polling
profile, so a slow background workflow cannot be mistaken for SSE admission
latency.

The deployed diagnostic revision records Redis publisher subscriber counts and
stream close/keepalive/byte counters in the compact artifact. The public 32-SSE
run `32970619144` then showed 7 HTTP 200 streams (connect p95 938ms), but Redis
reported `[0, 0, 0]` subscribers and 0/21 events while API CPU averaged 36.3%,
PostgreSQL 6.1% and lock contention was false. Code review found a relay
lifecycle race: closing one queue unconditionally closed the shared Redis
Pub/Sub resources for the remaining queues. The fix is covered by a regression
test and was verified by the exact-SHA release gate before repeating the public
staircase. The fixture was removed by exact cleanup `32970738275`,
which preserved `aleksei.lisitsin1@gmail.com` and left zero synthetic rows.

The public staircase after the relay fix is now measured on `8c0103e7`: the
32-SSE A/B delivered 24/24 events with event p95 40ms; the 1k run admitted
6 streams and delivered 18/18 events (connect p95 860ms); the 5k run admitted
4 and delivered 12/12 (connect p95 985ms); and the 10k run classified all
10,000 opens as one-second fallback with zero unexpected errors. API CPU was
not sustained at saturation in any of these public runs. Exact cleanups
`32972274118`, `32972607636`, `32972913089` and `32973507881` preserved the
control account and left zero synthetic rows.

The first 10k mixed SSE+polling attempt `32973657012` was canceled because the
load generator, not the VPS, reached 97.5% CPU while 10,000 polling tasks were
opened without a shared request gate. Abort `32974261688` and cleanup
`32974326125` removed its process tree and fixture. The harness now bounds
combined polling requests by the configured concurrency and cancels/drains
both workload tasks on timeout before the next mixed acceptance run.

The bounded mixed rerun on baseline `c4bcf3c9` (`32975890344`) reached the
VPS and stopped at its 85-second diagnostic budget with an explicit FAIL. It
recorded `deadlock-api` at 104.1% average CPU (152.8% peak), workspace origin
p95 879.9ms/p99 2.09s, PostgreSQL 17.3%, Redis 0.3%, and no lock or backend
wait contention. Its exact cleanup `32976226545` removed 10,000 users and one
tournament. The first single-tournament count-plan candidate (`99e465a3`,
run `32978141716`, cleanup `32978444085`) did not win this contour: API CPU
remained 104.0%, workspace server p95 was 1.09s and DB time rose slightly.
It was reverted from the follow-up candidate.

The next candidate skips the published-assignment lookup only for
`registration_open` detail workspaces. The domain guard makes assignment
staging unavailable before `registration_closed`; all access checks,
participant state, ETags, response fields and SSE admission remain unchanged.
The H2 live run `32979781513` (cleanup `32980077832`) reduced workspace SQL
from 6.0 to 5.0 queries/request and average DB time from 246ms to 201ms, but
did not yet lower the full server contour: workspace p95 was 909ms and API
CPU 107.1%. It remains a safe component improvement, not the final CPU winner.

H3 now measures the response boundary itself: workspace routes return the
already validated Pydantic payload through `model_dump_json()` with the same
ETag, avoiding a second response-model validation/JSON encoding pass. It is
accepted only with unchanged status/body/ETag contracts and lower API CPU and
workspace p95 on the same mixed contour.
The H3 live run `32981400437` (cleanup `32981700615`) reached the bounded
diagnostic budget with workspace p50/p95/p99 `383/996/2,070ms`, API CPU
`106.6%` average and `144.6%` peak, PostgreSQL `17.1%`, Redis `0.27%`, and no
lock/backend-wait contention. It did not beat H2 and is not the selected
winner. The next local H4 adds a 128-entry/2-second public registration-open
workspace snapshot for the lean detail polling contract, with fresh
permission checks and mutation invalidation. H4 is deployed from
`706dd0ab` after security/build `32982759177`, automatic deploy `32983323484`
and production deploy/live smoke `32983333250`. The first public mixed run
`32986215002` used a `600s` polling stagger instead of the H2/H3 `85s` contour,
returned three live-update `503`s and is excluded from H4 ranking; exact
cleanup `32986648873` removed 10,000 users and one tournament. The corrected
same-shape run `32986759039` reached the bounded `85s` diagnostic budget with
no polling request errors, but sustained API CPU remained `105.0%`. It reduced
workspace server p95 from H3 `996ms` to `743ms`, DB time from `198ms` to
`164ms` and SQL from `5.0` to `4.08/request`; exact cleanup
`32986913152` removed 10,000 users and one tournament and verified zero
synthetic users, tournaments, sessions and audit rows. H4 is the current
component-level winner, not a full combined-capacity pass; the remaining
ceiling is API CPU and the bounded mixed workload still does not complete.

AS-19 — SSE capacity and combined-load measurement is in progress. The reviewed
runner adds separate SSE-only and polling+SSE profiles with exact
cleanup/recovery support. Public tests send no source-bucket bypass: Cloudflare,
Nginx, Redis admission and application caps remain active. Failed runs export
compact server metrics; the ten hypotheses and five follow-ups are specified in
[`as-19-sse-capacity-benchmark.md`](as-19-sse-capacity-benchmark.md).

The local loopback origin staircase now passes the selected transport contour:
the worker/tournament relay plus coalesced authorization and a shared blocking
SSE limiter pool reached 1,000, 5,000 and 10,000 persistent streams with zero
HTTP errors and complete event delivery in the final 10k run. The final run
had 10,000/10,000 HTTP 200, 10,000/10,000 events, Redis peak 146, PostgreSQL
peak 31, no Redis rejection increase and no PostgreSQL connection growth with
active SSE. Its p95 connection-open latency was 309.4s, so handshake speed and
the combined polling+SSE profile remain release gates. This local result does
not authorize a 10,000-persistent-SSE claim: the exact-SHA CI/deploy gate and
 live smoke passed, but the first guarded live diagnostic stopped during
 authenticated fixture setup before any SSE opened. The prior per-stream Redis
 shape is rejected at 10k because it produced 18 limiter `503`s and Redis peak
 10,000. Combined public runs below open 32 SSE and therefore do not prove
 10,000 persistent SSE capacity.

The relay winner is deployed in `4f4b5863207e37cc2c9993be8d03f0cb72d10a66`;
security/build `32865611322`, automatic deploy `32866234695` and production
deploy/live smoke `32866244193` all passed. The first guarded live 10k SSE
diagnostic (`32866749952`) stopped before SSE at the fixture setup boundary:
the first authenticated CSRF request returned a Cloudflare 504 after about
30.2s. Exact cleanup (`32866955426`) deleted 10,000 synthetic users, left zero
fixture users/tournaments/sessions/audit rows and preserved
`aleksei.lisitsin1@gmail.com`. After compacting progress checkpoints and
redeploying as `3ed9d4a3`, repeat live run `32869459781` reached 10,000/10,000
HTTP 200 SSE responses with zero 429/503/other responses and zero client
errors. Connect p50/p95/p99 were 113.8s/202.0s/210.4s, with API CPU the
dominant signal (73.9% average, 136.2% peak) and no PostgreSQL lock
contention. The runner published before the final connection barrier and
delivered only 9 events, so this proves opening capacity only, not complete
fan-out. The next runner change adds an all-attempts barrier and strict event
delivery accounting before another live 10k fan-out run.
The opening-capacity repeat was cleaned by `32870304826`: 10,000 synthetic
users and 20 tournaments deleted, zero remaining users/tournaments/sessions/
audit rows, control account preserved.

The strict barrier/event-delivery runner was deployed as
`da203bd1f78dd52658b9a05f3964218266de094d` after security/build
`32871147286`, automatic deploy `32871730205` and production deploy/live smoke
`32871738322` passed. Its guarded 10k run `32872200332` did not reach SSE:
the same authenticated fixture-setup `GET /auth/csrf` returned Cloudflare 504
after about 30.2s, before tournament creation. This repeats the setup boundary
and is not evidence against the relay or SSE fan-out. Exact cleanup
`32872451869` deleted 10,000 users and verified zero remaining fixture users,
tournaments, sessions and audit rows while preserving the control account.
The next setup hypothesis refreshes PostgreSQL statistics after direct fixture
inserts and records bounded active PostgreSQL query samples during the run.
The retained-load supervisor now also records bounded live VPS snapshots
(`ps`, sockets, Redis client/rejection counters and PostgreSQL activity) plus
post-run API/worker journal and Nginx access/error tails; these are exported as
`server-observability.log` alongside the compact matrix artifact.
The ANALYZE diagnostic run `32874380384` exceeded the ten-minute observation
window and was stopped by exact abort `32875410391`; the first observer-enabled
rerun `32877021919` behaved the same and was stopped by `32878007693`. Neither
run produced a completed matrix summary, so neither is counted as an SSE
result. Exact cleanups `32875508695` and `32878057962` removed their
10,000-user/20-tournament fixtures and preserved the control account. The
abort path now also exports partial matrix/QA/server logs and an exact process
snapshot before cleanup; the observer samples every five seconds and bounds
journalctl to 4,000 records so diagnostics cannot create an unbounded
post-run wait.
The first complete observer-enabled 1,000-SSE diagnostic (`32879752207`) did
not hit a production admission limit: the load generator exited with
`IndexError: list index out of range` before writing a valid matrix summary.
Its server snapshots showed the generator at roughly 80--90% CPU, Redis
`rejected_connections` unchanged during the run, and no sampled PostgreSQL lock
wait. The runner is being hardened to preserve the full traceback, isolate
performance-summary failures and export the raw `qa-command.log`; cleanup
`32880184859` removed the exact fixture and preserved the control account.

The next observer-enabled 1,000-SSE run (`32881488410`) reached the application
cleanly: 1,000/1,000 connections returned HTTP 200, there were no errors,
429s or 503s, and all 1,000 expected events were delivered. Connect latency
was p50/p95/p99 8.72/17.77/18.00 seconds; event delivery latency was
395.5/574.2/584.7 ms. The run was still marked failed because the performance
collector crashed while summarizing PostgreSQL active-query samples. The
collector was fixed to keep its system-sample window separate from those
rows, with a regression test; exact cleanup `32882110537` removed the fixture
and preserved the control account. This is a valid 1k SSE transport result,
but not yet the final staircase gate until the repaired collector produces
complete telemetry.

The first 5,000-SSE public-origin run (`32883773066`) reached the VPS only
partially: 3,535 connections returned HTTP 200 and 1,465 returned Cloudflare
503 Error 1200 (`cache_connection_limit`, `Retry-After: 60`). Application
429s were zero, PostgreSQL lock contention was not observed, and sustained CPU
saturation was false; the red result is therefore an edge-capacity result,
not an origin-capacity result. Exact cleanup `32884651890` removed the 10k
fixture set and preserved the control account. The retained-load harness now
supports an explicit `origin-local` SSE mode (`127.0.0.1:8010` with the
canonical production Origin header) to measure the VPS origin without
Cloudflare; public mode remains the separate edge acceptance test.
The first controlled 5,000-SSE origin-local run (`32886113934`) accepted
5,000/5,000 connections and delivered 5,000/5,000 events with no errors;
connect latency was p50/p95/p99 29.4/89.3/95.7 seconds. This proves only that
the application can eventually hold the connections, not that the public
site can accept them fast enough. Its cleanup initially hit a harness
provenance guard because the report correctly recorded `127.0.0.1`; the
cleanup contract now allows only this SSE control mode when the request Origin
remains canonical.
The public ramp A/B with `open_concurrency=16` (`32888021777`) still failed at
the edge: 3,466/5,000 streams returned HTTP 200, 1,533 returned Cloudflare
1200 and one returned Cloudflare 502; the application returned zero 429s.
Connect latency was p50/p95/p99 38.0/93.0/143.1 seconds. VPS evidence again
showed no sustained CPU, PostgreSQL connection/lock or backend-wait pressure.
The exact cleanup removed 10,000 users and 20 tournaments and preserved the
control account. Deployed candidate `1d0f56d5` lowered application SSE
admission to 3,000 and makes the browser close failed EventSource connections
and use revision polling during a cooldown. Its public 5k run produced
3,000 HTTP 200 SSE plus 2,000 fast app 429s, zero Cloudflare 1200/503/errors
and all 3,000 events; successful-stream connect p95 was still 61.8s.
The public mixed 10k profile then exposed a separate revalidation burst. The
optional-read authentication A/B was deployed as `3d09aa0a`; it removed the
second `last_seen_at` checkout from read routes while preserving mutation
touches and SSE revalidation. Public 1k (`1787697719`) and 5k
(`1787698223`) combined runs passed through Cloudflare with zero errors and
exact cleanup. The 10k run with the default HTTP client pool of 40
(`1787698626`, cleaned by `1787698627`) also had zero errors, 304s and
Cloudflare/5xx/429/503 responses, but its client p95 was about 80.6s because
the load generator queued work behind its own 40-connection pool. The origin
was materially faster but API CPU averaged about 84% and workspace origin p95
was about 1.66s.

The corrected 10k public A/B with HTTP pool512 (`1787699600`, cleaned by
`1787699601`) removed generator queueing but averaged ~91% API CPU. The selected
lean-contour F2 (`1787700610`, cleaned by `1787700611`) passed 10,000 users with
11,659 polling requests, p95/p99 262/388ms, ~49.6% API CPU, 1,201 304s and
zero errors; F1 also passed. Final deployed gate `1787701010` repeated the
pass with p95/p99 277/420ms, 1,200 304s and zero errors. F3 pool256 failed with 9 pool-timeout 500s, F4
viewer `bracket_summary` passed but was slower (365/647ms, ~86.4% CPU), and
F5 server early workspace `304` failed with 20 errors. The F5 live patch was
removed, baseline hash restored and health returned 200. A local integration
test is present but `platformdb_test` credentials are invalid; CI remains the
release gate after the selected changes are committed.
The first valid 1k run reached the application but failed with 500s while
long-lived responses held request-scoped PostgreSQL connections; removing a
duplicate QA-only session lookup was not sufficient. The next focused A/B
explicitly closed the endpoint session and materially improved the result, but
router/auth dependencies still retained request-scoped sessions. A first
global `scope="function"` attempt was canceled at CI run `32829249835` after
an ordinary invite-claim path showed an `idle in transaction` session and a
transactionid lock wait. The corrected design keeps ordinary API dependencies
request-scoped and isolates function-scoped auth/policy/serialization in a
dedicated SSE router, with revalidation kept in short-lived sessions. The
first H1 on that corrected deployment (`32832475533`, cleaned by
`32832797705`) reached `200=620`/`500=380`, `max_active=422`, and connect p95
21.49s. PostgreSQL peaked at 195.0% CPU and the SSE server route p95 was
57.9s, with no PostgreSQL connection-peak or lock-wait flag. The runner now
records bounded non-200 response-body diagnostics. This is not a capacity
pass. A same-shape intermediate contour passed at 256 (`32834441834`, cleaned
by `32834698357`: 256/256, zero errors), while 512 failed (`32834773835`,
cleaned by `32835036424`: 415/512, 97 Cloudflare 500s, max_active 348). The
current reliable contour is 256. A 512/open128 backpressure A/B improved to
468/512 with 44 Cloudflare 500s (`32835148133`, cleaned by `32835407140`), but
still failed. Query reuse then improved the same contour to 486/512 with 26
Cloudflare 500s, 41 client errors, `max_active=445` and connect p95 10.83s in
`32837162933`; exact cleanup `32837600679` verified zero retained fixture
data and preserved `aleksei.lisitsin1@gmail.com`. This is the best measured
512/open128 contour, but it still fails the zero-unexpected-errors criterion;
the 10+5 ranking and staircase remain pending.
The first 5,000-SSE staircase (`32837747171`, exact cleanup `32838201646`)
was substantially worse at `open_concurrency=512`: `200=1,856/5,000`,
`500=3,143`, `errors=308`, `max_active=338`, zero events and connect p95
221.6s. No 429/503 admission response occurred; failures were sampled as
Cloudflare 500s. This confirms that the nominal Nginx/Redis ceilings are not
the current capacity and that opening pressure/origin work must be reduced
before 5k/10k targets are meaningful.
At 512 connections, a more gradual `open_concurrency=64` contour
(`32838425845`, cleanup `32838635589`) reached 512/512 HTTP 200 responses,
max_active 504 and 234 events with p95 opening 11.72s, but recorded 8 client
errors. It was near the boundary, not yet a strict pass; the subsequent
open32 run established the higher strict contour described below.
Reducing opening concurrency to 32 produced the first strict 512-SSE pass in
`32838825035` (cleanup `32839036740`): 512/512 HTTP 200, zero errors, 284
events and connect p95 12.30s, with no sustained CPU, PostgreSQL connection or
lock saturation. The same open32 shape at 1,000 SSE (`32839100405`, cleanup
`32839300689`) reached 990/1,000 with 17 errors and sustained `deadlock-api`
CPU saturation, so the current strict staircase point is 512. The follow-up
lifecycle A/B removes the duplicate pre-subscription authorization query and
changes idle checkpoints from every keepalive to a 30s cadence while retaining
mandatory revalidation before each private event. On the deployed
duplicate-check variant, 512/open64 became a strict pass (`32840244186`,
cleanup `32840480141`: 512/512, zero errors, 393 events, p95 10.86s);
1000/open32 still reached only 990/1000 with 16 errors and sustained API CPU
(`32840531009`, cleanup `32840726728`). The idle-checkpoint variant then
improved 1000/open32 to 989/1000 with 12 errors and removed sustained CPU
saturation (`32841823646`, cleanup `32842030758`), but did not meet the strict
gate. Reducing opening concurrency further to 16 returned sustained API CPU
and produced 13 errors (`32842094082`, cleanup `32842288384`). The current
strict contour is 512 with open32/open64; 1000 persistent SSE remains
unproven. The participant-snapshot A/B is deployed in `7105dde8` and retains
the same authorization and revocation semantics. Its first 1000/open32 run
(`32843777739`, cleanup `32844061492`) was invalid because the generator hit
`Errno 24: Too many open files`. The runner fix in `d5cd6f93` raises the SSE
child-process `nofile` limit and logs the effective values. The valid repeat
(`32845174078`, cleanup `32845451618`) reached 998/1000 HTTP 200 streams,
with two Cloudflare 500s, five incomplete-chunk client errors and connect p95
20.14s; there were no 429/503 responses, sustained CPU/load-average flags or
PostgreSQL connection/lock saturation. Stream DB work fell to 5.26 average
queries and 161.7ms DB time. Compact load artifacts now include the effective
`nofile` limits and bounded error/response samples. This is a material
improvement, but not a strict 1000 pass. The same-shape repeat
(`32846652953`, exact cleanup `32846866673`) reproduced the boundary failure
at 995/1000 HTTP 200 streams: five Cloudflare 500s, four incomplete-chunk
client errors, max_active 995, 401 events and connect p95 18.64s. The load
generator reported nofile 32768/32768; there were still no 429/503 responses,
sustained CPU/load-average flags or PostgreSQL connection/lock saturation.
This keeps the strict contour at 512. Cleanup verified 1,000 synthetic users
and 20 tournaments removed, zero fixture users, tournaments, sessions and
audit rows remaining, and preservation of `aleksei.lisitsin1@gmail.com`. The
next controlled comparison changes only opening concurrency (16 versus 64)
before any admission-limit change. Open16 (`32847009440`, cleanup
`32847218607`) reached 999/1000 HTTP 200 with one Cloudflare 500 and eight
incomplete-chunk errors. Open64 (`32847283128`, cleanup `32847524729`) reached
1000/1000 HTTP 200 with zero HTTP errors but eight incomplete-chunk errors.
Both retained nofile 32768/32768 and showed no 429/503, PostgreSQL connection
or lock saturation; neither is a strict pass. Investigation found the SSE
Nginx location's `proxy_read_timeout=60s` exactly matched the 60s benchmark
hold. The isolated `660s` candidate was deployed as `c3e5752f` and tested at
open64 in `32848671574` (cleanup `32848969483`): it regressed to 999/1000
HTTP 200, one Cloudflare 500 and 13 incomplete-chunk errors, with high load
average and PostgreSQL CPU. It is rejected and the SSE timeout is restored to
60s; no admission limit was changed.

**AS-16 — Test-suite audit and executable CI/live ownership** is resolved and
live-validated in production.
The audit remediation is tracked in [`test-suite-governance.md`](test-suite-governance.md):
deterministic backend/migration/web groups must run in CI, and production
browser QA must execute through the dedicated server wrapper.
The runtime release at `04d691b95b6ba9fde5982aed658523fe2e896407` passed the
security/build gate (`32589822458`), production deployment (`32590065060`) and
the full production browser gate (`32590276914`).

AS-15 — Deadlock persistence and workflow concurrency integrity is resolved and deployed. The release locks durable workflow/profile writers on
their stable parent rows, revalidates lifecycle state under lock, adds final
database guards and applies migration `20260822_0040`. Exact commit
`87525bab34c473ac51708eba1e242b7baa6a1462` is active as release
`gha-32574455599-1-87525bab34c4-20260822T125945Z`; closure evidence is in
[`archive/as-15-deadlock-workflow-integrity.md`](archive/as-15-deadlock-workflow-integrity.md).

### AS-19 SSE capacity and burst boundary — 2026-08-27

The protected package ending at `2c551c50` is deployed: signed HMAC tickets,
PostgreSQL-free ticketed opens, private-stream revalidation, shared
worker/tournament relay, SharedWorker deduplication, polling fallback and
fail-closed Redis global/source/user leases (`3,000/32/4`). The relay now
formats each event once into a bounded shared sequence buffer, avoiding a
synchronous queue fan-out loop for every subscriber. Application caps and
authorization semantics were not weakened.

Origin-local ticket capacity exceeded the 10,000-user target:

| SSE target | Result | Connect p95 / event p95 | Resource result |
| --- | --- | --- | --- |
| 15,000 | 15,000/15,000; 45,000/45,000 events; 0 errors | 1.46s / 7.71s | API avg/max 52.8/121.0%; cgroup peak 830MB |
| 17,000 | 17,000/17,000; 51,000/51,000 events; 0 errors | 2.18s / 5.55s | API 52.7/131.2%; cgroup peak 866MB |
| 20,000 | 20,000/20,000; 60,000/60,000 events; 0 errors | 2.85s / 6.52s | API 54.0/119.5%; cgroup peak 985MB |

The 20,000 run (`33034469879`, exact cleanup `33034798652`) is the highest
verified origin point and is near the current roughly 1GB API memory boundary;
no higher origin cap is claimed without a memory plan. Earlier 15,000 and
17,000 runs were `33032746181` / `33033042381` and `33034096316` /
`33034425381`. All exact cleanups verified zero fixture users, tournaments,
sessions and audit rows.

Public/Cloudflare ticket opening at the protected application cap of 3,000
passed without application errors at paced rates, but the handshake tail is
edge/transport-bound: rate 50/s reached 3,000/3,000 with connect p95 4.94s;
rate 75/s reached all 3,000 with p95 15.83s; rate 100/s reached all 3,000
with p95 24.14s. Event p95 stayed 1.42–1.66s and origin CPU/Redis/PostgreSQL
remained below saturation. The runs were `33035099910`, `33035798822` and
`33035593586`; exact cleanups were `33035266312`, `33036000968` and
`33035761732`. A 5,000-attempt overflow test (`33035307148`, cleanup
`33035561336`) respected the 3,000 cap: 3,000 connected, 193 received the
expected 429, and the rest timed out in the edge queue, with no 503 or
application error.

The mixed production contour also passed at 3,000 SSE plus 10,000 polling
users: run `33036740237`, cleanup `33037055264`, 3,000/3,000 HTTP 200,
9,000/9,000 events, 10,000/10,000 polling requests and zero errors. Connect
p95 was 12.33s and event p95 1.68s. The dominant measured resource was the
API at 80.9% average and 140.3% peak CPU with about 919MB peak RSS; PostgreSQL
and Redis had no lock/backend-wait or admission saturation. A 1,000-SSE
mixed control also passed (`33036365159`, cleanup `33036695877`).

The current safe boundary is therefore: origin-local 20,000 verified, public
3,000 protected and paced, and mixed 3,000 SSE plus 10,000 polling verified.
The remaining customer-facing bottleneck is Cloudflare/transport opening
queueing, while the next origin boundary is API memory/CPU. Raising the
application cap would remove deliberate backpressure without fixing either
boundary. Ten-thousand public persistent SSE and the exact 180% two-core
target remain unclaimed; further progress requires an operator-owned
edge/transport or VPS resource change followed by the same protected tests.

AS-17 — End-to-end release transaction and recovery is resolved and deployed.
The release receipt now has an explicit Nginx uncertainty boundary, idempotent
`activation-committed` completion, retained runtime recovery, and rollback
restoration of the previous release's code, venv, units and Nginx before
restart/smoke. Fault-injection coverage spans pointer/venv, units, Nginx,
restart, smoke, activation commit, receipt cleanup and the two-process crash
after rollback pointer switch through the old `current` helper. Recovery uses a
root-owned shared bundle and compatibility shim, while migration uncertainty
remains fail-closed and no automatic Alembic downgrade is added. Closure evidence is in
[`archive/as-17-release-transaction-recovery-2026-08-23.md`](archive/as-17-release-transaction-recovery-2026-08-23.md).
The final GitHub security/build gate (`32638426827`) passed before production
deployment `32638711370`; the deploy workflow now fails closed for any target
SHA without `platform-security-build=success`. The final runtime release is
`gha-32638711370-1-09574590cd80-20260823T121307Z`; the subsequent docs-only
release `gha-32639416463-1-7b7224feb13e-20260823T122756Z` was deployed through
the same gate and retained that runtime state. Server-side production
diagnostics passed for both release SHAs in runs `32639026796` and
`32639662616`; post-deploy content diagnostics (`32638938744`, `32639641466`)
and patch translation warm-up (`32638938742`, `32639641488`) also passed.
AS-12 has code-side fail-closed validation and a read-only parity gate, while
the VPS proof remains operator-owned. AS-13's CI contour is being revalidated
against the current web/api/worker identities and units.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only workspace reads require active participant membership or explicit organizer/admin authority; historical inactive participant rows are not authorization grants, including for an already-open private bracket SSE stream.
- Organizer exclusion must retain the tournament participant row as `disqualified`; self-rejoin and same-tournament invite redemption remain blocked until the organizer deliberately restores an active status. This is tournament-scoped and must not become a platform-wide ban.
- Invite use and participant capacity are transaction-scoped PostgreSQL invariants: invite claim/revoke locks the stable tournament and invite rows in tournament-to-invite order; ordinary joins claim durable free slots without locking the tournament row, while lifecycle/restore mutations retain the tournament-row boundary and recheck capacity. Authentication last-seen touches use an isolated database transaction and must never commit or release locks owned by a mutation request.
- Resource-creating API retries use durable actor/scope `Idempotency-Key` records. A repeated key with the same payload resolves to the originally created tournament/invite; reusing a key with a different payload is rejected.
- Player-commitment reconciliation is a tournament workflow writer: it locks every affected Tournament row in deterministic id order before reading lifecycle state or releasing commitments. Automation failure-state persistence reacquires the same Tournament lock after any rollback.
- Every Deadlock ready-check start/close, captain, assignment generation,
  roster publish and roster-lock write path — API, automation and worker alike
  — locks its tournament row before checking lifecycle state. Ordinary ready
  votes are the deliberate exception: they upsert the unique vote row and its
  32-way counter shard without taking the tournament-row lock; a deferred
  database guard rejects votes recorded after round closure or without active
  participation, while preserving a vote timestamped before the close commit.
  Redis may coalesce work but never replaces this durable transaction boundary.
- Participant capacity is represented by durable per-tournament slots. Join
  claims a free slot with `FOR UPDATE SKIP LOCKED`; inactive retained rows and
  deletes release capacity, while the unique `(tournament_id, user_id)` index
  and idempotency record guard retries. The table materializes a bounded
  inventory and allocates sparse rows above it on demand, so the permitted
  nine-digit API capacity cannot trigger a massive slot backfill.
- Bracket/workspace reads expose revision-derived private ETags and accept
  `If-None-Match`; unchanged reads return `304`. Active browser views poll at
  the existing short interval, hidden/passive/terminal views back off or stop,
  and SSE remains admission-limited.
- API and worker SQLAlchemy pools are explicit and bounded: the measured
  10k-polling baseline is API `2 x (16 + 0)` and worker `2 x (2 + 0)` within
  the ordinary 44-connection budget. Ticketed SSE admission does not consume
  PostgreSQL at open; unticketed legacy admission and periodic private
  revalidation use the bounded stream database pool. The separate SSE
  authorization pool was removed. Redis admission uses the bounded `512`
  connection pool with a `2s` wait and remains fail-closed.
  Celery uses high/default/low queues, prefetch one and late acks;
  backlog/retry pressure is part of the load evidence. The current mixed
  3,000-SSE plus 10,000-polling contour completed without pool checkout,
  lock or backend-wait errors; its remaining origin pressure was API CPU and
  memory, not SSE admission or Redis/PostgreSQL saturation. Any future
  increase must remeasure the ordinary workspace workload and the roughly
  1GB API memory boundary without weakening the bounded pools or fail-closed
  admission.
- The final 20×500 browser-polling gate keeps fixture state bounded to at most
  32 participants per tournament and uses four setup lanes plus one shared
  request semaphore. Its production runner retains 10,000 virtual tabs but
  uses HTTP40, a 300-second opening stagger and a 30-second mixed
  active/passive polling window. Its five-minute auto-assignment wait is
  fail-fast; the write-burst profile owns join/ready-vote contention
  measurements.
- Ready-check votes must be committed only while their round is active and the
  voter remains an eligible active participant. A close or exclusion cannot
  leave a post-close or ineligible vote in persistence.
- The database is the final concurrency guard for cardinal workflow state:
  active ready-checks and the selected captain/assignment/roster state must not
  have ambiguous concurrent rows even if a future writer bypasses a service.
- Dream-slot replacement is serialized on the owning profile/user row. A
  replace-all request leaves exactly its selected profile-level slots, never a
  merge of concurrent payloads; slot values remain in the supported range.
- Public API contracts are explicit allowlists. Account/contact email and Steam authentication identity do not belong to anonymous public-profile DTOs, participant moderation metadata belongs only to organizer-management DTOs, and public automation error fields must never contain arbitrary exception text. A future public email feature requires a separate explicit opt-in contract rather than reusing account contact data.
- Public bracket SSE must retain layered application/Nginx connection caps, fail closed when Redis-backed admission state is unavailable, release leases on normal termination and retain bounded expiry recovery after abnormal termination. Healthy streams have no artificial lifetime rotation: keepalive, renewable leases and access revalidation govern their lifetime, while reconnect admission remains bounded and fully jittered.
- Public media rendering must remain `R2 -> CDN -> browser`; normal API runtime must not proxy R2 objects, serve legacy upload paths or fall back to local-disk reads. Legacy URL columns and migration helpers may remain only while runtime-inert and migration/grace-period scoped.
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

- Remaining Cloudflare dashboard follow-up for CAA, WAF/rates and R2 settings where the operator checklist still marks work `VERIFY`/`TODO`.
- VPS-owned AS-12 evidence: loopback-only listeners, `FORWARDED_ALLOW_IPS=127.0.0.1`,
  exact Cloudflare CIDR parity across UFW/Nginx, and a direct-origin negative
  test. Repository checks do not prove live state.
- Real-user CSP follow-up and classification of new enforcement reports.
- Post-grace physical removal of runtime-inert legacy media URL columns/call-site plumbing and migration-only helpers when no longer required.
- Non-security feature expansion that does not remove a launch or production blocker.

For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md). For evidence and details, follow the task router in [`README.md`](README.md).
