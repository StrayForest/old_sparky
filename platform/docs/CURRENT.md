# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-08-25

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
- Public bracket SSE connection pressure is bounded in two layers: Redis-backed application leases enforce global, source and authenticated-user admission caps with fail-closed behavior, while Nginx adds coarse source/global connection caps. Rejections are observable and stream lifetime/reconnect behavior is bounded.
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

The measured local staircase reached 1,000 → 5,000 → 10,000 virtual users on
the selected bounded profile. Production remains commit- and exact-SHA-gated;
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
rows and preserved the control account. The required ten A/B experiments and
five follow-up variants are now complete; the selected profile is the bounded
active/passive mix with HTTP40, 300s opening stagger, 30s polling window and
API pool 12+0.
The load work required a ten-run controlled A/B matrix followed by five
follow-up variants around the winning configuration; that matrix is complete.
The winner was selected by zero errors/cleanup first, then latency, CPU and
pool wait, not by raw throughput alone.

AS-19 — SSE capacity and combined-load measurement is in progress. The
reviewed runner adds separate SSE-only and polling+SSE profiles to the existing
production retained-load workflow, with exact cleanup/recovery/abort support.
The protocol and acceptance rules are maintained in
[`as-19-sse-capacity-benchmark.md`](as-19-sse-capacity-benchmark.md). No
10,000-persistent-SSE claim is made until the 1k/5k/10k staircase and combined
run are measured through CI/CD.

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
  10k-polling baseline is API `2 x (12 + 0)` plus worker `2 x (2 + 0)` within a
  32-connection budget. Celery uses high/default/low queues, prefetch one and
  late acks; backlog/retry pressure is part of the load evidence.
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
- Public bracket SSE must retain layered application/Nginx connection caps, fail closed when Redis-backed admission state is unavailable, release leases on normal termination and retain bounded expiry recovery after abnormal termination. Stream lifetime and reconnect timing must remain bounded.
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
