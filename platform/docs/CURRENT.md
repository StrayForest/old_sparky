# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-09-02

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Web, API and worker run under separate locked Unix identities with per-service runtime environments; the web process receives no backend database, session, R2, mail, Turnstile-secret or OpenAI credentials.
- API and worker share only the dedicated `oldsparky-media` staging group; worker state and web cache remain service-owned.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, durable patch-translation state, the rebuildable tournament catalog read model with keyset pagination, the admin roster control center, immutable releases and tested rollback.
- Frontend audit remediation for contract validation, permissions, auth/session states, async draft/search races, retry boundaries, internal navigation and i18n is resolved and deployed in release `frontend-audit-remediation-20260822T204107Z`; evidence is in [`archive/frontend-audit-remediation-2026-08-22.md`](archive/frontend-audit-remediation-2026-08-22.md).
- Cloudflare Access now protects `/platform-ops*` and `/api/v1/admin*` with an operator-scoped Allow policy and independent MFA; a fresh incognito login verified the identity -> TOTP MFA -> application path while application `admin`/`superadmin` RBAC remains authoritative.
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
- Unknown public patch IDs return from the cache path without awaiting external content refresh. Per-ID negative caching and a Redis-coalesced global background-refresh gate bound miss amplification, while miss-triggered upstream requests refuse redirects and enforce a response-size limit.
- Password-login guessing protection uses independent source-IP and account-wide Redis state. Account identifiers are represented by HMAC fingerprints, shared failures drive adaptive Turnstile and a bounded cooldown, and successful login clears account failure/cooldown state.
- Production Alembic head is `20260901_0051`, including the tournament catalog
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

Current status: migration `20260901_0051` is the deployed Alembic head. The
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
short-TTL origin cache headers. A live probe on 2026-09-02 verified the
Cloudflare Cache Rule with `CF-Cache-Status: MISS` followed by `HIT` and
`Age: 0` on the public catalog response; an equivalent cookie-bearing probe
also reached an edge HIT. Cloudflare caching remains enabled, while
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

The 2026-09-02 authenticated read-mix A/B run improved 30,000-request wall time
from 784.5 s to 473.2 s and reduced raw p95 from 7.11 s to 3.98 s. Both API
cores still reached roughly 100%, so further read-path changes must be proven
against the same 20,000-user/40×500 profile without weakening authorization,
ETag correctness or exact cleanup.
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

- Remaining Cloudflare dashboard follow-up for CAA, WAF/rates and R2 settings where the operator checklist still marks work `VERIFY`/`TODO`.
- VPS-owned AS-12 evidence: loopback-only listeners, `FORWARDED_ALLOW_IPS=127.0.0.1`,
  exact Cloudflare CIDR parity across UFW/Nginx, and a direct-origin negative
  test. Repository checks do not prove live state.
- Real-user CSP follow-up and classification of new enforcement reports.
- Physical removal of persisted legacy media URL fields and migration-only helpers after production data and external-consumer inventory confirms that no migration or compatibility dependency remains; this requires a reviewed API/schema migration.
- Non-security feature expansion that does not remove a launch or production blocker. For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md); for evidence and details, follow [`README.md`](README.md).
